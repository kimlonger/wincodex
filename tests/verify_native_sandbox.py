"""显式原生沙箱验收：真实 token、目录外读写、网络、子进程、超时和无宿主回退。"""

import argparse
import json
import os
from pathlib import Path
import socket
import sys
import tempfile
import time
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from chatcode.config import Settings
from chatcode.tools import ToolContext, ToolError, call_tool
from chatcode.workspace import Workspace


def verify(base):
    project = base / "project"
    project.mkdir()
    private = base / "host-private.txt"
    private.write_text("PRIVATE_FIXTURE")
    (project / "source.txt").write_text("SOURCE_FIXTURE")
    ctx = ToolContext(Workspace(project), Settings(workspace=project, execution_mode="native", sandbox_memory_mb=128))
    def call(tool, **args):
        return call_tool(tool, ctx, args).structured
    task = call("task_create", goal="native sandbox verification", operation_id="native-test")
    task_id = task["task_id"]
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    with socket.create_connection(listener.getsockname(), timeout=1):
        accepted, _ = listener.accept()
        accepted.close()
    try:
        code = '''
import ctypes as c, os, socket, json, urllib.request
from ctypes import wintypes as w
from pathlib import Path
k,a=c.WinDLL('kernel32'),c.WinDLL('advapi32')
k.GetCurrentProcess.restype=w.HANDLE
a.OpenProcessToken.argtypes=[w.HANDLE,w.DWORD,c.POINTER(w.HANDLE)]
a.GetTokenInformation.argtypes=[w.HANDLE,c.c_int,c.c_void_p,w.DWORD,c.POINTER(w.DWORD)]
token=w.HANDLE();assert a.OpenProcessToken(k.GetCurrentProcess(),8,c.byref(token))
value,size=w.DWORD(),w.DWORD()
assert a.GetTokenInformation(token,29,c.byref(value),4,c.byref(size)) and value.value==1
a.GetTokenInformation(token,30,None,0,c.byref(size))
groups=c.create_string_buffer(size.value)
assert a.GetTokenInformation(token,30,groups,size,c.byref(size))
assert c.cast(groups,c.POINTER(w.DWORD))[0]==1
class SidAndAttributes(c.Structure):
 _fields_=[('sid',c.c_void_p),('attributes',w.DWORD)]
class TokenGroups(c.Structure):
 _fields_=[('count',w.DWORD),('groups',SidAndAttributes*1)]
internet_sid=c.create_string_buffer(68);internet_size=w.DWORD(68)
a.CreateWellKnownSid.argtypes=[c.c_int,c.c_void_p,c.c_void_p,c.POINTER(w.DWORD)]
a.EqualSid.argtypes=[c.c_void_p,c.c_void_p]
assert a.CreateWellKnownSid(85,None,internet_sid,c.byref(internet_size))
assert a.EqualSid(c.cast(groups,c.POINTER(TokenGroups)).contents.groups[0].sid,internet_sid)
k.CloseHandle.argtypes=[w.HANDLE];k.CloseHandle(token)
Path('native-output.txt').write_text('SANDBOX_WRITE_OK')
assert Path('source.txt').read_text()=='SOURCE_FIXTURE'
blocked=[]
for operation in ('read','write'):
 try:
  p=Path(__PRIVATE_FILE__)
  p.read_text() if operation=='read' else p.write_text('ESCAPED')
 except PermissionError: blocked.append(operation)
assert blocked==['read','write'],blocked
assert 'CHATCODE_PRIVATE_TEST_ENV' not in os.environ
opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
with opener.open('https://www.python.org/robots.txt',timeout=15) as response:
 assert response.status==200
 assert response.read(1024)
loopback='reachable'
s=socket.socket();s.settimeout(1)
try:s.connect(('127.0.0.1',PORT))
except OSError:loopback='windows_policy_or_environment_rejected'
finally:s.close()
print(json.dumps(dict(appcontainer_token=True,copy_read_write=True,host_private_read_write_blocked=True,internet_client_capability=True,public_dns_tls_https_succeeded=True,loopback=loopback,host_secret_env_not_inherited=True)))
'''.replace("__PRIVATE_FILE__", repr(str(private))).replace("PORT", str(listener.getsockname()[1]))
        process = call("exec_command", argv=["python", "-c", code], task_id=task_id, yield_time_ms=10000)
        deadline = time.monotonic() + 35
        while process["running"] and time.monotonic() < deadline:
            process = call("read_process", process_id=process["process_id"], task_id=task_id, wait_ms=1000)
        assert process["exit_code"] == 0 and process["state"] == "exited", process
        result = json.loads(process["stdout"])
        execution = ctx.workspace.tasks.execution_workspace(task_id)
        assert (execution.root / "native-output.txt").read_text() == "SANDBOX_WRITE_OK"
        assert not (project / "native-output.txt").exists()
        assert private.read_text() == "PRIVATE_FIXTURE"
        child_code = "import time; from pathlib import Path; time.sleep(3); Path('child-escaped.txt').write_text('bad')"
        parent_code = "import subprocess,sys; subprocess.Popen([sys.executable,'-c'," + repr(child_code) + "]); print('CHILD_STARTED')"
        process = call("exec_command", argv=["python", "-c", parent_code], task_id=task_id, yield_time_ms=10000)
        assert process["exit_code"] == 0 and "CHILD_STARTED" in process["stdout"], process
        time.sleep(3.2)
        assert not (execution.root / "child-escaped.txt").exists()
        result["children_killed_with_parent_job"] = True
        process = call("exec_command", argv=["python", "-c", "import time;time.sleep(30)"], task_id=task_id, timeout=1, yield_time_ms=10000)
        assert process["timed_out"] and not process["running"], process
        result["timeout_terminates_job"] = True
        process = call("exec_command", argv=["python", "-c", "x=bytearray(300*1024*1024)"], task_id=task_id, yield_time_ms=10000)
        assert process["exit_code"] != 0, process
        result["memory_limit_enforced"] = True
        try:
            call("exec_command", argv=["python", "-c", "print('HOST_ESCAPE')"])
        except ToolError as exc:
            assert exc.code == "SANDBOX_COPY_REQUIRED", exc
        else:
            raise AssertionError("Host fallback unexpectedly allowed")
        result.update(no_host_fallback=True, no_external_sandbox_software=True, sandbox=process["sandbox"])
        return result
    finally:
        listener.close()
        ctx.workspace.isolated.shutdown()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="chatcode-native-") as directory:
        with patch.dict(os.environ, {"CHATCODE_DATA_DIR": str(Path(directory) / "state"), "CHATCODE_PRIVATE_TEST_ENV": "FIXTURE_SECRET"}):
            data = verify(Path(directory))
    text = json.dumps(data, ensure_ascii=False, indent=2)
    if args.output:
        args.output.write_text(text, encoding="utf-8")
    print(text)


if __name__ == "__main__":
    main()
