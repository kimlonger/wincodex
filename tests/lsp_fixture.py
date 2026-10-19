"""用于协议边界测试的 stdio 对端；不作为产品语言服务。"""

import json
from pathlib import Path
import re
import sys
from urllib.request import url2pathname
from urllib.parse import urlparse


def send(message):
    raw = json.dumps(message).encode()
    sys.stdout.buffer.write(f"Content-Length: {len(raw)}\r\n\r\n".encode() + raw)
    sys.stdout.buffer.flush()


def point(text, at):
    before = text[:at]
    return {"line": before.count("\n"), "character": len(before.rsplit("\n", 1)[-1].encode("utf-16-le")) // 2}


def range_(text, start, end):
    return {"start": point(text, start), "end": point(text, end)}


root, mode = Path.cwd(), sys.argv[1] if len(sys.argv) > 1 else "normal"
documents, versions = {}, {}
apply_rejected = False
rename_count = 0
while True:
    headers = {}
    while True:
        line = sys.stdin.buffer.readline()
        if not line:
            sys.exit()
        if line == b"\r\n":
            break
        key, value = line.decode().split(":", 1)
        headers[key.lower()] = value.strip()
    message = json.loads(sys.stdin.buffer.read(int(headers["content-length"])))
    method, args = message.get("method"), message.get("params", {})
    if method == "exit":
        break
    if method == "initialize":
        send({"jsonrpc": "2.0", "id": "server-edit", "method": "workspace/applyEdit", "params": {"edit": {"changes": {}}}})
        result = {"capabilities": {"textDocumentSync": 1, "documentSymbolProvider": True,
                                   "referencesProvider": True, "renameProvider": True}}
    elif method == "initialized":
        continue
    elif message.get("id") == "server-edit":
        apply_rejected = message.get("result", {}).get("applied") is False
        continue
    elif method in ("textDocument/didOpen", "textDocument/didChange"):
        doc = args["textDocument"]
        documents[doc["uri"]] = doc.get("text") if method.endswith("didOpen") else args["contentChanges"][0]["text"]
        versions[doc["uri"]] = doc["version"]
        continue
    elif method == "textDocument/documentSymbol":
        text = documents[args["textDocument"]["uri"]]
        start, end = text.index("function foo"), text.index("}") + 1
        result = [{"name": "foo", "kind": 12, "range": range_(text, start, end),
                   "selectionRange": range_(text, start + 9, start + 12)}]
    elif method in ("textDocument/rename", "textDocument/references"):
        if method.endswith("rename"):
            rename_count += 1
        edits, locations = {}, []
        for path in sorted(root.glob("*")):
            if path.suffix not in (".py", ".js"):
                continue
            uri = path.resolve().as_uri()
            text = documents.get(uri, path.read_text(encoding="utf-8-sig"))
            items = []
            for match in re.finditer(r"\bfoo\b", text):
                area = range_(text, match.start(), match.end())
                locations.append({"uri": uri, "range": area})
                items.append({"range": area, "newText": args.get("newName", "")})
            if items:
                edits[uri] = items
        if method.endswith("references"):
            result = locations
        elif not apply_rejected:
            raise RuntimeError("客户端没有拒绝服务端主动写入")
        elif mode == "outside":
            edits[(root.parent / "outside.py").as_uri()] = next(iter(edits.values()))
            result = {"changes": edits}
        elif mode == "version":
            result = {"documentChanges": [{"textDocument": {"uri": uri, "version": -99}, "edits": items} for uri, items in edits.items()]}
        elif mode == "overlap":
            for items in edits.values():
                items.append(items[0])
            result = {"changes": edits}
        else:
            if mode == "external" and rename_count >= 2:
                (root / "b.py").write_bytes(b"# external edit\n")
            result = {"changes": edits}
    elif method == "shutdown":
        result = None
    elif "id" not in message:
        continue
    else:
        send({"jsonrpc": "2.0", "id": message["id"], "error": {"code": -32601, "message": "unsupported"}})
        continue
    send({"jsonrpc": "2.0", "id": message["id"], "result": result})
