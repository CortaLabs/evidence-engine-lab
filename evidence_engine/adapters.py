"""Pure, deliberately narrow adapters. Roles and identifiers are observations."""
import json
from typing import Any, Protocol


class Adapter(Protocol):
    adapter_id: str
    adapter_version: str

    def parse_line(self, raw: bytes, *, source_key: str,
                   line_no: int) -> list[dict[str, Any]]: ...


def event(kind, text, *, role=None, tool=None, call_id=None, occurred_at=None,
          extra=None, classification="recognized"):
    return dict(kind=kind, text=text, role=role, tool=tool, call_id=call_id,
                occurred_at=occurred_at, extra=extra or {}, classification=classification)


def required(obj, key):
    value = obj.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError("invalid field")
    return value


def reject_constant(value):
    raise ValueError("Non-finite JSON")


def blocks(value):
    if isinstance(value, str):
        return [value]
    if not isinstance(value, list):
        raise ValueError("invalid content")
    texts = []
    for block in value:
        if not isinstance(block, dict) or block.get("type") not in {
            "input_text", "output_text", "text"
        } or not isinstance(block.get("text"), str):
            raise ValueError("unsupported content block")
        texts.append(block["text"])
    return texts


def text_parts(value):
    """Keep known neighbors when a future block type appears."""
    if isinstance(value, str):
        return [value], False
    if not isinstance(value, list):
        raise ValueError()
    texts, unsupported = [], False
    for block in value:
        if not isinstance(block, dict):
            raise ValueError()
        if block.get("type") in {"input_text", "output_text", "text"}:
            if not isinstance(block.get("text"), str):
                raise ValueError()
            texts.append(block["text"])
        else:
            unsupported = True
    return texts, unsupported


def diagnostic():
    return [event("diagnostic", "Malformed record shape", classification="malformed")]


class CodexAdapter:
    adapter_id = "codex_native_rollout"
    adapter_version = "1"

    def parse_line(self, raw, *, source_key, line_no):
        try:
            obj = json.loads(raw, parse_constant=reject_constant)
            if not isinstance(obj, dict):
                raise ValueError()
            timestamp = obj.get("timestamp")
            if timestamp is not None and not isinstance(timestamp, str):
                raise ValueError()
            payload = obj.get("payload")
            if obj.get("type") == "session_meta":
                sid = required(payload, "id")
                return [event("session", "Session metadata", occurred_at=timestamp,
                              extra={"session_id": sid})]
            if obj.get("type") != "response_item":
                return [event("unknown", "Unsupported record", classification="unsupported")]
            if not isinstance(payload, dict):
                raise ValueError("response_item requires an object payload")
            kind = payload.get("type")
            if kind == "message":
                role = payload.get("role")
                if role not in {"system", "developer", "user", "assistant", "tool"}:
                    raise ValueError()
                content = payload.get("content")
                if not isinstance(content, list):
                    raise ValueError()
                mapped = []
                for block in content:
                    texts, unsupported = text_parts([block])
                    mapped.extend(event("message", text, role=role, occurred_at=timestamp) for text in texts)
                    if unsupported:
                        mapped.append(event("unknown", "Unsupported message block", classification="unsupported"))
                return mapped or [event("unknown", "Empty message", classification="unsupported")]
            if kind in {"function_call", "custom_tool_call"}:
                name, cid = required(payload, "name"), required(payload, "call_id")
                args = required(payload, "arguments" if kind == "function_call" else "input")
                return [event("tool_call", args, role="assistant", tool=name, call_id=cid,
                              occurred_at=timestamp, extra={"native_type": kind})]
            if kind in {"function_call_output", "custom_tool_call_output"}:
                cid = required(payload, "call_id")
                raw_output = payload.get("output")
                if kind == "function_call_output" and not isinstance(raw_output, str):
                    raise ValueError()
                texts, unsupported = text_parts(raw_output)
                output = "\n".join(texts)
                mapped = [event("tool_result", output, role="tool", call_id=cid,
                              occurred_at=timestamp, extra={"native_type": kind,
                                  "truncated": bool(payload.get("truncated", False)) or unsupported})]
                if unsupported:
                    mapped.append(event("unknown", "Unsupported output block", classification="unsupported"))
                return mapped
            return [event("unknown", "Unsupported response item", classification="unsupported")]
        except (ValueError, TypeError, AttributeError, UnicodeError, RecursionError):
            return diagnostic()


class ClaudeAdapter:
    """Synthetic fixture mapping, NOT live Claude compatibility certification."""
    adapter_id = "claude_code_jsonl"
    adapter_version = "1"

    def parse_line(self, raw, *, source_key, line_no):
        try:
            obj = json.loads(raw, parse_constant=reject_constant)
            if not isinstance(obj, dict):
                raise ValueError()
            if obj.get("type") not in {"user", "assistant"}:
                return [event("unknown", "Unsupported Claude record", classification="unsupported")]
            message = obj.get("message")
            role = required(message, "role")
            if role != obj["type"]:
                raise ValueError()
            timestamp = obj.get("timestamp")
            if timestamp is not None and not isinstance(timestamp, str):
                raise ValueError()
            content = message.get("content")
            if isinstance(content, str):
                content = [{"type": "text", "text": content}]
            if not isinstance(content, list) or not content:
                raise ValueError()
            result = []
            for block in content:
                kind = block.get("type")
                if kind == "text":
                    result.append(event("message", required(block, "text"), role=role, occurred_at=timestamp))
                elif kind == "tool_use" and role == "assistant":
                    args = block.get("input")
                    if not isinstance(args, dict):
                        raise ValueError()
                    result.append(event("tool_call", json.dumps(args, separators=(",", ":"), ensure_ascii=False),
                                        role=role, tool=required(block, "name"), call_id=required(block, "id"),
                                        occurred_at=timestamp))
                elif kind == "tool_result" and role == "user":
                    texts, unsupported = text_parts(block.get("content"))
                    result.append(event("tool_result", "\n".join(texts), role="tool",
                                        call_id=required(block, "tool_use_id"), occurred_at=timestamp,
                                        extra={"truncated": bool(block.get("truncated", False)) or unsupported,
                                               **({"is_error": block["is_error"]} if isinstance(block.get("is_error"), bool) else {})}))
                    if unsupported:
                        result.append(event("unknown", "Unsupported result block", classification="unsupported"))
                else:
                    result.append(event("unknown", "Unsupported Claude content block", classification="unsupported"))
            sid = obj.get("sessionId")
            if sid is not None:
                if not isinstance(sid, str):
                    raise ValueError()
                # Store validates this native identity, without copying it into every block.
                result.insert(0, event("session", "Session metadata", occurred_at=timestamp,
                                       extra={"session_id": sid}))
            return result
        except (ValueError, TypeError, AttributeError, UnicodeError, RecursionError):
            return diagnostic()
