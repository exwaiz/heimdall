"""Summarize the installed schema, preserving the generated originals."""
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def audit():
    root = ROOT / "schemas"
    result = {"files": {}, "contracts": {}, "client_methods": [], "server_requests": []}
    for path in sorted(root.rglob("*.json")):
        result["files"][str(path.relative_to(root))] = hashlib.sha256(path.read_bytes()).hexdigest()
    for name in ("InitializeParams", "ThreadListParams", "ThreadReadParams", "ThreadStartParams",
                 "ThreadResumeParams", "TurnStartParams", "TurnInterruptParams",
                 "ThreadTurnsListParams", "ThreadItemsListParams",
                 "CommandExecutionRequestApprovalResponse", "FileChangeRequestApprovalResponse"):
        matches = list(root.rglob(name + ".json"))
        if not matches:
            result["contracts"][name] = {"available": False}
            continue
        schema = json.loads(matches[0].read_text())
        definitions = schema.get("definitions", {})
        result["contracts"][name] = {
            "available": True, "path": str(matches[0].relative_to(root)),
            "required": schema.get("required", []), "properties": schema.get("properties", {}),
            "policy_definitions": {k: v for k, v in definitions.items()
                                   if k in ("SandboxMode", "SandboxPolicy", "AskForApproval",
                                            "ApprovalsReviewer", "ThreadSourceKind", "ThreadSortKey",
                                            "CommandExecutionApprovalDecision", "FileChangeApprovalDecision")},
        }
    for filename, key in (("ClientRequest.json", "client_methods"), ("ServerRequest.json", "server_requests")):
        schema = json.loads((root / filename).read_text())
        for entry in schema.get("oneOf", schema.get("anyOf", [])):
            result[key].extend(entry.get("properties", {}).get("method", {}).get("enum", []))
    return result


if __name__ == "__main__":
    print(json.dumps(audit(), ensure_ascii=False, indent=2))

