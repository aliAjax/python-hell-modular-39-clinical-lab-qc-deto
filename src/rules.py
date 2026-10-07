from .domain import ConflictError, InvalidTransition, PermissionDenied, ValidationError


def _find_one(lookup, kind, field, value):
    if lookup is None:
        return None
    rows = lookup(kind, field, value) or []
    return rows[0] if rows else None


def calibration_is_valid(calibration_due, as_of):
    return str(calibration_due)[:10] >= str(as_of)[:10]


def evaluate_qc(history, value, target, sd, config=None):
    """Evaluate one QC value against numeric and multi-rule criteria."""
    config = dict(config or {})
    try:
        value = float(value)
        target = float(target)
        sd = float(sd)
    except (TypeError, ValueError):
        raise ValidationError("qc value, target and sd must be numeric")
    if sd <= 0:
        raise ValidationError("qc sd must be positive")
    limit = float(config.get("limit_sd", 3.0))
    bias_n = int(config.get("consecutive_n", 4))
    bias_sd = float(config.get("consecutive_sd", 1.0))
    trend_n = int(config.get("trend_n", 4))
    z_score = round((value - target) / sd, 4)
    flags = []
    if abs(z_score) > limit:
        flags.append("1_3s")
    values = [float(item) for item in history] + [value]
    if len(values) >= bias_n:
        window = values[-bias_n:]
        if all(item > target + bias_sd * sd for item in window):
            flags.append("bias_high")
        if all(item < target - bias_sd * sd for item in window):
            flags.append("bias_low")
    if len(values) >= trend_n:
        window = values[-trend_n:]
        if all(window[index] < window[index + 1] for index in range(len(window) - 1)):
            flags.append("trend_up")
        if all(window[index] > window[index + 1] for index in range(len(window) - 1)):
            flags.append("trend_down")
    passed = not flags
    return {
        "accepted": passed,
        "flags": flags,
        "z_score": z_score,
        "rule_snapshot": {
            "limit_sd": limit,
            "consecutive_n": bias_n,
            "consecutive_sd": bias_sd,
            "trend_n": trend_n,
        },
    }


def unrecovered_rejection(batches):
    return [batch for batch in batches if batch.get("status") == "intercepted"]


def bridge_is_effective(bridge, as_of):
    """A confirmed bridge whose validity window covers the result time."""
    if not bridge or bridge.get("status") != "confirmed":
        return False
    data = bridge.get("data") or {}
    as_of = str(as_of or "")
    valid_from = str(data.get("valid_from") or "")
    expires_at = str(data.get("expires_at") or "")
    if valid_from and as_of < valid_from:
        return False
    if expires_at and as_of > expires_at:
        return False
    return True


def _bridges_for(lookup, assay_id, instrument_id):
    return [
        bridge
        for bridge in (lookup("lot_bridge", "assay_id", assay_id) or [])
        if bridge["data"].get("instrument_id") == instrument_id
    ]


def _bridge_release_stamp(bridge, as_of):
    status = bridge["status"]
    if status == "pending":
        raise ConflictError("bridge coverage is waiting for authorizer confirmation")
    if status == "suspended":
        raise ConflictError("bridge suspended after new lot QC failure: release is stopped")
    if status == "expired":
        raise ConflictError("bridge coverage expired: renew and reconfirm coverage before release")
    if not bridge_is_effective(bridge, as_of):
        raise ConflictError("bridge coverage is not valid at result time")
    return {
        "release_mode": "bridge",
        "bridge_id": bridge["id"],
        "bridge_version": bridge["version"],
        "bridge_key": bridge["data"]["coverage_key"],
    }


def _validate_assay(actor, data, lookup):
    try:
        low = float(data.get("allowed_low"))
        high = float(data.get("allowed_high"))
    except (TypeError, ValueError):
        raise ValidationError("allowed_low and allowed_high must be numeric")
    if low >= high:
        raise ValidationError("allowed_low must be less than allowed_high")
    return {
        "rule_config": dict(data.get("rule_config") or {}),
    }


def _validate_qc_lot(actor, data, lookup):
    if not _find_one(lookup, "assay", "id", data.get("assay_id")):
        raise ValidationError("assay does not exist")
    try:
        target = float(data.get("target"))
        sd = float(data.get("sd"))
    except (TypeError, ValueError):
        raise ValidationError("target and sd must be numeric")
    if sd <= 0:
        raise ValidationError("sd must be positive")
    duplicate = _find_one(lookup, "qc_lot", "lot_key", "%s:%s" % (data["assay_id"], data["lot_no"]))
    if duplicate:
        raise ConflictError("qc lot already exists for assay")
    return {"lot_key": "%s:%s" % (data["assay_id"], data["lot_no"]), "target": target, "sd": sd}


def _validate_instrument(actor, data, lookup):
    if not str(data.get("serial", "")).strip():
        raise ValidationError("instrument serial is required")
    return {"calibration_due": data.get("calibration_due")}


def _validate_qc_run(actor, data, lookup):
    assay = _find_one(lookup, "assay", "id", data.get("assay_id"))
    lot = _find_one(lookup, "qc_lot", "id", data.get("qc_lot_id"))
    instrument = _find_one(lookup, "instrument", "id", data.get("instrument_id"))
    if not assay or not lot or not instrument:
        raise ValidationError("assay, qc lot and instrument are required")
    if lot["data"].get("assay_id") != assay["id"]:
        raise ValidationError("qc lot does not belong to the assay")
    try:
        value = float(data.get("value"))
    except (TypeError, ValueError):
        raise ValidationError("qc result value must be numeric")
    return {"value": value}


def _validate_lot_bridge(actor, data, lookup):
    assay = _find_one(lookup, "assay", "id", data.get("assay_id"))
    instrument = _find_one(lookup, "instrument", "id", data.get("instrument_id"))
    new_lot = _find_one(lookup, "qc_lot", "id", data.get("new_lot_id"))
    previous_lot = _find_one(lookup, "qc_lot", "id", data.get("previous_lot_id"))
    if not assay or not instrument or not new_lot or not previous_lot:
        raise ValidationError("assay, instrument, new qc lot and previous qc lot are required")
    if new_lot["data"].get("assay_id") != assay["id"] or previous_lot["data"].get("assay_id") != assay["id"]:
        raise ValidationError("both lots must belong to the bridge assay")
    if new_lot["id"] == previous_lot["id"]:
        raise ValidationError("new lot and previous lot must differ")
    if not str(data.get("expires_at", "")).strip():
        raise ValidationError("expires_at is required")
    coverage_key = "%s:%s:%s" % (instrument["id"], assay["id"], new_lot["id"])
    for bridge in _bridges_for(lookup, assay["id"], instrument["id"]):
        if bridge["data"].get("new_lot_id") == new_lot["id"] and bridge["status"] in (
            "pending",
            "confirmed",
            "expired",
        ):
            raise ConflictError("bridge coverage already exists for this instrument/assay/lot")
    return {
        "coverage_key": coverage_key,
        "valid_from": data.get("valid_from"),
    }


def _validate_result_batch(actor, data, lookup):
    if not _find_one(lookup, "assay", "id", data.get("assay_id")):
        raise ValidationError("assay does not exist")
    if not _find_one(lookup, "instrument", "id", data.get("instrument_id")):
        raise ValidationError("instrument does not exist")
    if not _find_one(lookup, "qc_run", "id", data.get("qc_run_id")):
        raise ValidationError("qc run does not exist")
    if int(data.get("patient_count", 0)) < 0:
        raise ValidationError("patient_count cannot be negative")
    return {}


def _validate_evaluate(actor, entity, data, lookup):
    assay = _find_one(lookup, "assay", "id", entity["data"].get("assay_id"))
    lot = _find_one(lookup, "qc_lot", "id", entity["data"].get("qc_lot_id"))
    if not assay or not lot:
        raise ValidationError("assay or qc lot disappeared")
    previous = []
    for run in lookup("qc_run", "instrument_id", entity["data"].get("instrument_id")) or []:
        if run["id"] == entity["id"] or run["status"] not in ("accepted", "rejected"):
            continue
        if run["data"].get("qc_lot_id") != entity["data"].get("qc_lot_id"):
            continue
        if str(run["data"].get("run_at", "")) < str(entity["data"].get("run_at", "")):
            previous.append(run["data"]["value"])
    result = evaluate_qc(
        previous,
        entity["data"].get("value"),
        lot["data"].get("target"),
        lot["data"].get("sd"),
        assay["data"].get("rule_config"),
    )
    if not result["accepted"] and not data.get("reject_reason"):
        result["reject_reason"] = "quality control rule violation"
    result["_next_status"] = "accepted" if result["accepted"] else "rejected"
    return result


def _validate_release(actor, entity, data, lookup):
    run = _find_one(lookup, "qc_run", "id", entity["data"].get("qc_run_id"))
    instrument = _find_one(lookup, "instrument", "id", entity["data"].get("instrument_id"))
    if not run or run["status"] != "accepted":
        raise ConflictError("result batch can only be released with an accepted QC run")
    if not instrument or instrument["status"] != "ready":
        raise ConflictError("instrument is not ready")
    if not calibration_is_valid(instrument["data"].get("calibration_due"), entity["data"].get("run_at")):
        raise ConflictError("instrument calibration is not valid at result time")
    active_holds = []
    for batch in lookup("result_batch", "instrument_id", entity["data"].get("instrument_id")) or []:
        if batch["id"] != entity["id"] and batch["status"] == "intercepted":
            active_holds.append(batch)
    if active_holds:
        raise ConflictError("an intercepted result batch must be resolved first")
    stamp = _release_coverage_stamp(entity, run, lookup)
    stamp["released_by"] = actor.user_id
    return stamp


def _release_coverage_stamp(entity, run, lookup):
    """Resolve how a patient result batch is covered after QC lot changes.

    Releasing against the previous (borrowed) lot is only accepted while an
    authorized bridge is effective for the instrument/assay/new-lot triple.
    Legacy data upgraded to ``pending_bridge`` can never reuse the old
    conclusion either.
    """
    assay_id = entity["data"].get("assay_id")
    instrument_id = entity["data"].get("instrument_id")
    run_lot_id = run["data"].get("qc_lot_id")
    result_at = entity["data"].get("run_at")
    candidate = None
    for bridge in _bridges_for(lookup, assay_id, instrument_id):
        if bridge["data"].get("previous_lot_id") != run_lot_id:
            continue
        if candidate is None or bridge["created_at"] >= candidate["created_at"]:
            candidate = bridge
    superseded = any(
        str(lot["data"].get("replaces_lot_id") or "") == str(run_lot_id)
        for lot in (lookup("qc_lot", "assay_id", assay_id) or [])
    )
    needs_bridge = entity["status"] == "pending_bridge" or candidate is not None or superseded
    if not needs_bridge:
        return {"release_mode": "normal"}
    if candidate is None:
        raise ConflictError(
            "no bridge coverage for the borrowed lot: an authorized bridge must be confirmed first"
        )
    stamp = _bridge_release_stamp(candidate, result_at)
    stamp["borrowed_lot_id"] = run_lot_id
    # An upgraded legacy batch is re-released through a real bridge now;
    # the old conclusion is not carried over.
    stamp["bridge_upgrade"] = False
    return stamp


def _validate_qc_retest(actor, entity, data, lookup):
    replacement = _find_one(lookup, "qc_run", "id", data.get("replacement_run_id"))
    if not replacement or replacement["status"] != "accepted":
        raise ValidationError("a replacement run must exist and be accepted")
    if replacement["data"].get("assay_id") != entity["data"].get("assay_id"):
        raise ValidationError("replacement run belongs to another assay")
    return {"replacement_run_id": replacement["id"]}


def _validate_switch_lot(actor, entity, data, lookup):
    previous = _find_one(lookup, "qc_lot", "id", data.get("previous_lot_id"))
    if not previous or previous["status"] != "active":
        raise ValidationError("previous active lot is required")
    if previous["data"].get("assay_id") != entity["data"].get("assay_id"):
        raise ValidationError("lots must belong to the same assay")
    return {"replaces_lot_id": previous["id"], "switched_at": data.get("switched_at")}


def _validate_bridge_confirm(actor, entity, data, lookup):
    if not data.get("authorizer_id"):
        raise ValidationError("authorizer_id is required")
    return {"confirmed_by": actor.user_id}


def _validate_bridge_expire(actor, entity, data, lookup):
    return {"reason": data.get("reason") or "bridge validity window elapsed"}


def _validate_bridge_suspend(actor, entity, data, lookup):
    if not data.get("cutoff_at"):
        raise ValidationError("cutoff_at is required")
    if not data.get("reason"):
        raise ValidationError("reason is required")
    if not _find_one(lookup, "qc_run", "id", data.get("failure_run_id")):
        raise ValidationError("failure_run_id must reference an existing qc run")
    return {}


def _validate_bridge_renew(actor, entity, data, lookup):
    if not data.get("expires_at"):
        raise ValidationError("expires_at is required")
    return {}


def _validate_correct(actor, entity, data, lookup):
    if not data.get("reason"):
        raise ValidationError("correction reason is required")
    history = list(entity["data"].get("correction_history") or [])
    history.append({"actor_id": actor.user_id, "reason": data["reason"], "from_status": entity["status"]})
    return {"correction_history": history}


class RuleEngine:
    ALIASES = {
        "assays": "assay",
        "qc_lots": "qc_lot",
        "instruments": "instrument",
        "qc_runs": "qc_run",
        "result_batches": "result_batch",
        "lot_bridges": "lot_bridge",
    }
    INITIAL_STATUS = {
        "assay": "active",
        "qc_lot": "registered",
        "instrument": "ready",
        "qc_run": "pending",
        "result_batch": "waiting",
        "lot_bridge": "pending",
    }
    TRANSITIONS = {
        "assay": {
            "suspend": (("active",), "suspended"),
            "restore": (("suspended",), "active"),
        },
        "qc_lot": {
            "activate": (("registered", "suspended"), "active"),
            "switch_in": (("registered",), "active"),
            "suspend": (("active",), "suspended"),
            "retire": (("active", "suspended"), "retired"),
        },
        "instrument": {
            "calibrate": (("ready", "maintenance", "failed"), "ready"),
            "fail": (("ready",), "failed"),
            "maintain": (("ready", "failed"), "maintenance"),
            "restore": (("maintenance", "failed"), "ready"),
        },
        "qc_run": {
            "evaluate": (("pending",), "pending"),
            "retest": (("rejected",), "retesting"),
            "investigate": (("rejected",), "investigated"),
            "resolve": (("investigated", "retesting"), "resolved"),
            "correct": (("accepted", "rejected", "investigated", "resolved"), "pending"),
        },
        "result_batch": {
            "release": (("waiting", "pending_bridge"), "released"),
            "intercept": (("waiting",), "intercepted"),
            "retest": (("intercepted",), "waiting"),
            "investigate": (("intercepted",), "investigating"),
            "resolve": (("investigating",), "resolved"),
            "correct": (("waiting", "intercepted", "investigating", "released", "resolved"), "waiting"),
        },
        "lot_bridge": {
            "confirm": (("pending",), "confirmed"),
            "expire": (("confirmed",), "expired"),
            "reconfirm": (("expired",), "pending"),
            "stop": (("confirmed", "suspended"), "suspended"),
        },
    }
    CREATE_REQUIRED = {
        "assay": ("name", "unit", "allowed_low", "allowed_high"),
        "qc_lot": ("assay_id", "lot_no", "target", "sd", "expires_at"),
        "instrument": ("name", "serial", "calibration_due"),
        "qc_run": ("assay_id", "qc_lot_id", "instrument_id", "value", "run_at"),
        "result_batch": ("assay_id", "instrument_id", "qc_run_id", "run_at", "patient_count"),
        "lot_bridge": (
            "assay_id",
            "instrument_id",
            "new_lot_id",
            "previous_lot_id",
            "expires_at",
        ),
    }
    ACTION_REQUIRED = {
        ("assay", "suspend"): ("reason",),
        ("qc_lot", "switch_in"): ("previous_lot_id", "switched_at"),
        ("qc_lot", "suspend"): ("reason",),
        ("qc_lot", "retire"): ("reason",),
        ("instrument", "calibrate"): ("calibration_due", "certificate_id"),
        ("instrument", "fail"): ("reason",),
        ("instrument", "maintain"): ("reason",),
        ("qc_run", "evaluate"): ("evaluated_by",),
        ("qc_run", "retest"): ("reason",),
        ("qc_run", "investigate"): ("reason",),
        ("qc_run", "resolve"): ("resolution",),
        ("qc_run", "correct"): ("reason", "value"),
        ("result_batch", "release"): ("reviewer_id",),
        ("result_batch", "intercept"): ("reason",),
        ("result_batch", "retest"): ("replacement_run_id", "reason"),
        ("result_batch", "investigate"): ("reason",),
        ("result_batch", "resolve"): ("resolution",),
        ("result_batch", "correct"): ("reason",),
        ("lot_bridge", "confirm"): ("authorizer_id",),
        ("lot_bridge", "stop"): ("cutoff_at", "reason", "failure_run_id"),
        ("lot_bridge", "reconfirm"): ("expires_at", "authorizer_id"),
    }
    CREATE_ROLES = {
        "assay": ("supervisor", "admin"),
        "qc_lot": ("supervisor", "admin"),
        "instrument": ("supervisor", "admin"),
        "qc_run": ("operator", "supervisor", "admin"),
        "result_batch": ("operator", "supervisor", "admin"),
        "lot_bridge": ("supervisor", "admin"),
    }
    ROLE_ACTIONS = {
        "suspend": ("supervisor", "admin"),
        "restore": ("supervisor", "admin"),
        "activate": ("supervisor", "admin"),
        "switch_in": ("supervisor", "admin"),
        "retire": ("supervisor", "admin"),
        "calibrate": ("supervisor", "admin"),
        "fail": ("operator", "supervisor", "admin"),
        "maintain": ("operator", "supervisor", "admin"),
        "evaluate": ("operator", "supervisor", "admin"),
        "retest": ("operator", "supervisor", "admin"),
        "investigate": ("supervisor", "admin"),
        "resolve": ("supervisor", "admin"),
        "correct": ("supervisor", "admin"),
        "release": ("supervisor", "admin"),
        "intercept": ("operator", "supervisor", "admin"),
        "confirm": ("supervisor", "admin"),
        "expire": ("supervisor", "admin"),
        "reconfirm": ("supervisor", "admin"),
        "stop": ("supervisor", "admin"),
    }
    CUSTOM_CREATE = {
        "assay": _validate_assay,
        "qc_lot": _validate_qc_lot,
        "instrument": _validate_instrument,
        "qc_run": _validate_qc_run,
        "result_batch": _validate_result_batch,
        "lot_bridge": _validate_lot_bridge,
    }
    CUSTOM_TRANSITIONS = {
        ("qc_run", "evaluate"): _validate_evaluate,
        ("result_batch", "release"): _validate_release,
        ("result_batch", "retest"): _validate_qc_retest,
        ("qc_lot", "switch_in"): _validate_switch_lot,
        ("qc_run", "correct"): _validate_correct,
        ("result_batch", "correct"): _validate_correct,
        ("lot_bridge", "confirm"): _validate_bridge_confirm,
        ("lot_bridge", "expire"): _validate_bridge_expire,
        ("lot_bridge", "stop"): _validate_bridge_suspend,
        ("lot_bridge", "reconfirm"): _validate_bridge_renew,
    }

    def normalize_kind(self, kind):
        return self.ALIASES.get(kind, kind)

    def initial_status(self, kind):
        kind = self.normalize_kind(kind)
        if kind not in self.INITIAL_STATUS:
            raise ValidationError("unknown kind: " + str(kind))
        return self.INITIAL_STATUS[kind]

    @staticmethod
    def _ensure_role(actor, allowed):
        if actor.role not in allowed:
            raise PermissionDenied("role %s is not allowed here" % actor.role)

    @staticmethod
    def _require(data, fields):
        for field in fields:
            value = data.get(field)
            if value is None or value == "" or value == [] or value == {}:
                raise ValidationError("missing required field: " + field)

    def validate_create(self, actor, kind, data, lookup=None):
        kind = self.normalize_kind(kind)
        if kind not in self.INITIAL_STATUS:
            raise ValidationError("unknown kind: " + str(kind))
        self._ensure_role(actor, self.CREATE_ROLES.get(kind, ("admin",)))
        self._require(data, self.CREATE_REQUIRED.get(kind, ()))
        custom = self.CUSTOM_CREATE.get(kind)
        return custom(actor, data, lookup) if custom else {}

    def validate_transition(self, actor, entity, action, data, lookup=None):
        kind = self.normalize_kind(entity["kind"])
        transition = self.TRANSITIONS.get(kind, {}).get(action)
        if not transition:
            raise InvalidTransition("unknown action %s for %s" % (action, kind))
        allowed_statuses, next_status = transition
        if entity["status"] not in allowed_statuses:
            raise InvalidTransition("cannot %s from status %s" % (action, entity["status"]))
        allowed_roles = self.ROLE_ACTIONS.get((kind, action), self.ROLE_ACTIONS.get(action, ("admin",)))
        self._ensure_role(actor, allowed_roles)
        self._require(data, self.ACTION_REQUIRED.get((kind, action), ()))
        custom = self.CUSTOM_TRANSITIONS.get((kind, action))
        extra = custom(actor, entity, data, lookup) if custom else {}
        if extra.get("_next_status"):
            next_status = extra.pop("_next_status")
        patch = dict(data)
        if extra:
            patch.update(extra)
        return next_status, patch
