"""Step 7 — Evaluation: weight of evidence + human feedback.

weight_of_evidence(alert, ...) scores how much the pipeline should trust its
own alert, combining four signals:

- source weight: an 8-K is the company's own disclosure (1.0); a news
  headline is secondhand (0.8)
- severity and confidence from the validated detection
- corroboration: independent items in the same batch reporting the event
- consistency with the obligor's trajectory (escalation from an already
  elevated profile is more credible than a cold-start spike)

Alerts landing in the gray zone (WoE between the bands) are routed to the
human-feedback hook — auto-approve by default; wire `HITLManager`
(evoagentx.hitl) here for interactive approve/reject/edit.
"""

SEVERITY_WEIGHT = {"critical": 1.0, "high": 0.8, "medium": 0.55,
                   "low": 0.25, "positive": 0.15, None: 0.0}
SOURCE_WEIGHT = {"8k": 1.0, "news": 0.8}
GRAY_ZONE = (0.35, 0.65)   # WoE band routed to human review


def weight_of_evidence(alert: dict, corroboration: int = 1,
                       context: dict = None) -> float:
    det = alert.get("detection", {})
    w_source = SOURCE_WEIGHT.get(alert.get("source"), 0.6)
    w_severity = SEVERITY_WEIGHT.get(det.get("severity"), 0.0)
    try:
        confidence = float(det.get("confidence") or 0.0)
    except (TypeError, ValueError):
        confidence = 0.0
    confidence = min(max(confidence, 0.0), 1.0)
    w_corrob = min(1.0, 0.5 + 0.25 * max(0, corroboration - 1))

    w_consistency = 0.7  # neutral default
    profile = (context or {}).get("profile") or {}
    trajectory = profile.get("trajectory") or []
    if trajectory:
        last = trajectory[-1]
        prev = str(last.get("risk_level") or last.get("level") or "low")
        escalation_order = ["low", "medium", "high", "critical"]
        new_level = str(alert.get("decision", {}).get("risk_level", "low"))
        if (prev in escalation_order and new_level in escalation_order
                and escalation_order.index(new_level)
                >= escalation_order.index(prev)):
            w_consistency = 0.9    # continuing/escalating an open story
        else:
            w_consistency = 0.6    # de-escalation against history: discount

    woe = (0.30 * w_severity + 0.25 * confidence + 0.15 * w_source
           + 0.15 * w_corrob + 0.15 * w_consistency)
    return round(woe, 3)


class AutoApprover:
    """Default human-feedback hook: record the routing, decide nothing."""

    def __call__(self, alert: dict):
        woe = alert.get("weight_of_evidence", 0.0)
        if GRAY_ZONE[0] <= woe <= GRAY_ZONE[1]:
            return {"routed_to": "human", "mode": "auto-timeout-approve",
                    "woe": woe}
        return None


def make_hitl_hook(hitl_manager):
    """Adapter for evoagentx.hitl.HITLManager (async, CLI approve/reject).

    Usage:
        from evoagentx.hitl.approval_manager import HITLManager
        hook = make_hitl_hook(HITLManager(...))
    """
    import asyncio
    from evoagentx.hitl.hitl import (HITLDecision, HITLInteractionType,
                                     HITLMode)

    def hook(alert: dict):
        woe = alert.get("weight_of_evidence", 0.0)
        if not (GRAY_ZONE[0] <= woe <= GRAY_ZONE[1]):
            return None
        resp = asyncio.get_event_loop().run_until_complete(
            hitl_manager.request_approval(
                task_name="credit_risk_alert",
                agent_name="decide_agent",
                action_name="alert",
                interaction_type=HITLInteractionType.APPROVE_REJECT,
                mode=HITLMode.POST_EXECUTION,
                action_inputs_data={"alert": alert},
                execution_result=alert.get("decision"),
                workflow_goal="credit_risk_monitoring",
                timeout=300))
        return {"routed_to": "human",
                "approved": resp.decision == HITLDecision.APPROVE,
                "feedback": resp.feedback, "woe": woe}

    return hook
