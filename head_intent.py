"""Head-call grounding for the isolated evaluation copy; no device access.

Compose object/action evidence instead of requiring complete command phrases.
Negations are action-scoped and checked even when a model supplies evidence.
"""
import re

OBJECT = r"(?:头部|脑袋|脑壳|脑门|头|云台|镜头|视线)"
GAP = r"(?:(?!然后|接着|随后|再|恢复)[^，。！？!?、；;]){0,8}?"
PATTERNS = {
    "up": rf"{OBJECT}{GAP}(?:抬|仰|升|向上|往上|朝上)|(?:抬|仰|升){GAP}{OBJECT}|(?:向上|往上|朝上)看|看上方|看高处|往高处看|升起|视线.{0,4}高|镜头.{0,4}高",
    "down": rf"{OBJECT}{GAP}(?:低|垂|降|向下|往下|朝下)|(?:低|垂|降){GAP}{OBJECT}|(?:向下|往下|朝下)看|看地面|看下方|降下",
    "level": rf"{OBJECT}{GAP}(?:放平|回正|摆正|恢复平|水平|正前方)|平视|水平|放平|回正|摆正|看正前方|恢复正常角度",
    "angle": r"[零一二两三四五六七八九十百\d]+(?:点[零一二两三四五六七八九\d]+)?度",
}
NEGATIVE = r"(?:不要|别|不用|无需|不需要|不想|不许|禁止)"


def head_positions(text, action):
    pattern = PATTERNS.get(action)
    if not pattern:
        return []
    return [m.start() for m in re.finditer(pattern, text)
            if not re.search(rf"{NEGATIVE}.{{0,8}}$", text[:m.start()])
            and not re.search(NEGATIVE, m.group())]


def check_head_intent(arguments, user_text, legacy_evidence=False):
    action = str(arguments.get("action") or "").strip().lower()
    if action not in PATTERNS:
        return False, "head_action_invalid"
    if action == "angle":
        angle = arguments.get("angle")
        if isinstance(angle, bool) or not isinstance(angle, int) or not 0 <= angle <= 360:
            return False, "head_angle_missing_or_invalid"

    requested, forbidden = set(), set()
    # Keep punctuation until clause separation: a prohibition on one action
    # must not prohibit another action in the next clause.
    clauses = re.split(r"[，。！？!?、；;]|然后|接着|随后|但是|而是|但|请", user_text)
    for clause in clauses:
        text = re.sub(r"\s+", "", clause)
        if re.search(rf"{NEGATIVE}.{{0,6}}(?:动|调整|转动).{{0,4}}{OBJECT}", text):
            return False, "explicitly_negated_action"
        if re.search(rf"{OBJECT}.{{0,4}}{NEGATIVE}.{{0,4}}(?:动|调整|转动)", text):
            return False, "explicitly_negated_action"
        for candidate, pattern in PATTERNS.items():
            for match in re.finditer(pattern, text):
                before = text[:match.start()]
                # Also handle object-first negatives: 脑袋不要抬起来.
                negated = bool(re.search(rf"{NEGATIVE}.{{0,8}}$", before)
                               or re.search(NEGATIVE, match.group()))
                (forbidden if negated else requested).add(candidate)
    if action in forbidden:
        return False, "explicitly_negated_action"
    if action in requested:
        return True, "head_compositional_evidence"
    if requested:
        return False, "head_action_conflict"
    if legacy_evidence:
        return True, "head_legacy_evidence"
    # Lack of familiar wording is not a proven conflict. Ask for clarification.
    return False, "head_intent_needs_confirmation"
