"""受控Python进程入口。应用层审计白名单，不是原生代码/OS沙箱。"""
from __future__ import annotations
import ipaddress
import ctypes  # 先初始化CPython自身的Windows错误处理，不授予脚本额外加载系统库的权限。
import json
import os
from pathlib import Path
import runpy
import socket
import subprocess
import sys


def inside(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def main():
    # -I忽略PYTHONIOENCODING；Windows重定向管道仍可能采用GBK。
    for stream in (sys.stdout,sys.stderr):
        if hasattr(stream,'reconfigure'):
            stream.reconfigure(encoding='utf-8',errors='replace')
    policy_path = Path(sys.argv[1]).resolve()
    script = Path(sys.argv[2]).resolve()
    arguments = sys.argv[3:]
    policy = json.loads(policy_path.read_text(encoding='utf-8'))
    workspace = Path(policy['workspace']).resolve()
    temp = Path(policy['temp']).resolve()
    application = Path(__file__).resolve().parents[2]
    backend = application / 'backend'
    runtime = Path(sys.base_prefix).resolve()
    skill_roots = [Path(value).resolve() for value in policy['skill_roots']]
    vendor = backend / 'vendor'
    bootstrap = Path(__file__).resolve()
    network_scripts = {Path(value).resolve() for value in policy['network_scripts']}
    network = script in network_scripts
    session = Path(policy['session']).resolve() if policy.get('session') else None
    fonts = [Path(value).resolve() for value in policy['fonts']]
    approved_ips: set[str] = set()
    spawn_active = False
    approved_command = None
    registry_handles=set()
    registry_pending=False
    raw_popen = subprocess.Popen
    raw_getaddrinfo = socket.getaddrinfo

    def resolve(value):
        if isinstance(value, int):
            return None
        return Path(os.fsdecode(value)).resolve()

    def file_access(value, write=False):
        path = resolve(value)
        if path is None:
            return
        if path.name in policy['blocked_names'] or path.name.startswith('.env.') or path.suffix.lower() in {'.pem', '.key'}:
            raise PermissionError('敏感文件不在运行白名单')
        if write:
            allowed = inside(path,temp) or (policy['write'] and inside(path,workspace))
        else:
            allowed = (inside(path,workspace) or inside(path,runtime) or inside(path,vendor)
                       or any(inside(path,root) for root in skill_roots+fonts)
                       or path in {bootstrap,policy_path,application,application/'config.py',backend/'security'/'policy.json'}
                       or (inside(path,backend) and (path.is_dir() or path.suffix in {'.py','.pyc','.pyd'}))
                       or (session is not None and inside(path,session)))
        if not allowed:
            raise PermissionError(('写入' if write else '读取')+'不在运行白名单：'+str(path))

    def approved_script(path):
        path = Path(path).resolve()
        if path.suffix != '.py':
            raise PermissionError('只允许Python脚本子进程')
        if inside(path,workspace):
            return path
        if any(inside(path,root/'scripts') for root in skill_roots):
            return path
        if inside(path,vendor) and policy['vendor_execution']:
            return path
        raise PermissionError('脚本不在角色白名单：'+str(path))

    def allowed_host(value):
        host = str(value).rstrip('.').lower()
        return any(host==item or host.endswith('.'+item) for item in policy['hosts']) or any(host.endswith(item) for item in policy['academic_suffixes'])

    def guarded_dns(host, port, *args, **kwargs):
        if not network or not allowed_host(host) or int(port or 443) not in policy['ports']:
            raise PermissionError('当前脚本或检索域名不在网络白名单')
        rows = raw_getaddrinfo(host,port,*args,**kwargs)
        if not rows or any(not ipaddress.ip_address(row[4][0]).is_global for row in rows):
            raise PermissionError('禁止连接非公网地址')
        approved_ips.update(row[4][0] for row in rows)
        return rows

    def guarded_popen(args, *positional, **kwargs):
        nonlocal spawn_active, approved_command
        if isinstance(args,(str,bytes)) or kwargs.get('shell') or len(args)<2:
            raise PermissionError('不允许shell或非Python子进程')
        if Path(args[0]).resolve()!=Path(sys.executable).resolve() or str(args[1]).startswith('-'):
            raise PermissionError('子进程只允许当前Python解释器和脚本文件')
        child = approved_script(args[1])
        command = [sys.executable,'-I','-B',str(bootstrap),str(policy_path),str(child),*map(str,args[2:])]
        # 固定安全环境，调用方不能通过PYTHONPATH或预加载选项替换启动器。
        kwargs['env'] = dict(os.environ)
        approved_command = command
        spawn_active = True
        try:
            return raw_popen(command,*positional,**kwargs)
        finally:
            spawn_active = False
            approved_command = None

    def audit(event,args):
        nonlocal registry_pending
        if event=='open':
            flags = args[2] or 0
            mode = args[1] or ''
            write = bool(flags & (os.O_WRONLY|os.O_RDWR|os.O_CREAT|os.O_TRUNC|os.O_APPEND)) or any(c in str(mode) for c in 'wax+')
            file_access(args[0],write)
        elif event in {'os.listdir','os.scandir'}:
            file_access(args[0] if args and args[0] is not None else os.getcwd())
        elif event in {'os.remove','os.rmdir','os.mkdir','os.chmod','os.utime','os.truncate'}:
            file_access(args[0],True)
            if event=='os.rmdir' and resolve(args[0])==workspace:
                raise PermissionError('不能删除项目根目录')
        elif event in {'os.rename'}:
            file_access(args[0],True);file_access(args[1],True)
        elif event=='os.chdir':
            file_access(args[0])
        elif event.startswith('os.startfile') or event in {'socket.sendto','socket.sendmsg','socket.gethostbyname','socket.gethostbyaddr','socket.getnameinfo'}:
            raise PermissionError('入口不在运行白名单：'+event)
        elif event in {'os.link','os.symlink','os.system','os.exec','os.posix_spawn','os.spawn','socket.bind'}:
            raise PermissionError('操作不在运行白名单：'+event)
        elif event=='subprocess.Popen':
            command=args[1]
            matches = (list(command)==approved_command if isinstance(command,(list,tuple)) else
                       isinstance(command,str) and approved_command is not None and command==subprocess.list2cmdline(approved_command))
            if not spawn_active or not matches:
                raise PermissionError('子进程必须继承同一运行白名单')
        elif event=='_winapi.CopyFile2':
            file_access(args[0]);file_access(args[1],True)
        elif event.startswith('_winapi.') and not spawn_active:
            raise PermissionError('禁止直接调用原生进程/文件接口：'+event)
        elif event=='socket.getaddrinfo':
            if not network or not allowed_host(args[0]):
                raise PermissionError('DNS请求不在运行白名单')
        elif event=='socket.connect':
            address=args[1]
            if not network or not isinstance(address,tuple) or address[0] not in approved_ips or address[1] not in policy['ports']:
                raise PermissionError('连接目标不在已核验的公网白名单')
        elif event=='ctypes.dlopen':
            if args[0] is None:
                raise PermissionError('不允许取得当前进程原生句柄')
            path=resolve(args[0])
            if not path.is_absolute() or not inside(path,runtime):
                raise PermissionError('原生库不在运行时白名单')
        elif event=='ctypes.dlsym':
            library,symbol=args
            name=str(getattr(library,'_name','')).lower()
            if name in {'kernel32','kernel32.dll'} and symbol not in {'GetLastError','GetModuleHandleW','GetModuleFileNameW','GetCurrentProcessId'}:
                raise PermissionError('系统原生符号不在白名单')
        elif event=='import' and len(args)>1 and args[1] and str(args[1]).lower().endswith(('.pyd','.so','.dll')):
            if not inside(Path(args[1]).resolve(),runtime):
                raise PermissionError('不允许工作区加载原生扩展')
        elif event.startswith('winreg.'):
            if event=='winreg.ConnectRegistry':
                if args[0] is not None:raise PermissionError('不允许远程注册表')
            elif event=='winreg.OpenKey':
                key=str(args[1] or '').lower()
                if args[2] & (2|4|0x10000|0x40000|0x80000):raise PermissionError('注册表只允许读')
                if not any(value in key for value in ['fonts','time zones','timezoneinformation']) and args[0] not in registry_handles:
                    raise PermissionError('只允许查询字体与时区注册信息')
                registry_pending=True
            elif event=='winreg.OpenKey/result':
                if not registry_pending:raise PermissionError('注册表句柄未获准')
                registry_handles.add(args[0]);registry_pending=False
            elif event in {'winreg.QueryValue','winreg.EnumValue','winreg.EnumKey','winreg.QueryInfoKey'}:
                if args[0] not in registry_handles:raise PermissionError('注册表句柄不在白名单')
            elif not event.endswith('/result'):
                raise PermissionError('注册表操作不在白名单')


    approved_script(script)
    socket.getaddrinfo = guarded_dns
    subprocess.Popen = guarded_popen
    sys.dont_write_bytecode = True
    sys.addaudithook(audit)
    sys.path.insert(0,str(script.parent))
    sys.path.insert(0,str(application))
    sys.argv=[str(script),*arguments]
    runpy.run_path(str(script),run_name='__main__')


if __name__=='__main__':
    main()
