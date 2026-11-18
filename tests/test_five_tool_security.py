"""五工具接口、角色权限与Python应用层白名单的回归验证。"""
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from backend.orchestration.orchestrate import build_registry, upload_tool
from backend.security.policy import check_url, ApprovedRedirect
from backend.tools.run_code import run_one
from backend.utils import paths
from config import Settings


class FiveToolSecurityTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.root = Path(self.folder.name)
        self.registry = build_registry(Settings(), object())

    def tearDown(self):
        self.folder.cleanup()

    def execute(self, source, role='engineer'):
        (self.root/'probe.py').write_text(source, encoding='utf-8')
        runner = self.registry.nodes[role].toolbox.runner(workspace=self.root)
        return runner('run_code', {'targets':[{"file_path": "probe.py",'timeout':40}]})

    def test_only_five_tools_registered_and_legacy_files_removed(self):
        expected = {'read','write','move','delete','run_code'}
        self.assertFalse(self.registry.errors)
        for role, node in self.registry.nodes.items():
            self.assertEqual(set(node.toolbox.names), {'read'} if role=='mentor' else expected)
        tools = Path(__file__).resolve().parents[1]/'backend/tools'
        for name in ['bash','figure','search','fetch','document']:
            self.assertFalse((tools/(name+'.py')).exists())
        with self.assertRaisesRegex(ValueError,'白名单'):
            upload_tool(self.root,'','anything','extra.py')

    def test_mentor_has_no_execution_tool(self):
        text, failed = self.execute("open('forbidden.txt','w').write('x')", 'mentor')
        self.assertTrue(failed, text)
        self.assertFalse((self.root/'forbidden.txt').exists())
        self.assertIn('research-review', self.registry.nodes['engineer'].toolbox.skill_names)
        (self.root/'audit.json').write_text(json.dumps({'path':'probe.py','operation':'fingerprint'}), encoding='utf-8')
        runner = self.registry.nodes['engineer'].toolbox.runner(workspace=self.root)
        text, failed = runner('run_code', {'targets':[{"file_path": "skill://research-review/scripts/audit.py", "args": ["--input", "audit.json", "--base-dir", "."]}]})
        self.assertFalse(failed, text)
        self.assertIn('sha256', text)


    def test_file_secret_and_process_escape_are_rejected(self):
        (self.root/'.env').write_text('fake fixture',encoding='utf-8')
        outside = self.root.parent/('outside-'+self.root.name+'.txt')
        cases = [
            "open('.env').read()",
            f"open({str(outside)!r},'w').write('x')",
            "import os; os.system('echo forbidden')",
            "import subprocess; subprocess.run(['cmd','/c','echo forbidden'])",
            "import subprocess,sys; subprocess.run([sys.executable,'-c','print(1)'])",
            "import os; os.link('probe.py','linked.py')",
            "import socket; socket.create_connection(('127.0.0.1',80),timeout=1)",
            "import socket; socket.socket(socket.AF_INET,socket.SOCK_DGRAM).sendto(b'fixture',('127.0.0.1',9))",
            "import socket; socket.gethostbyname('python.org')",
        ]
        for source in cases:
            with self.subTest(source=source):
                text, failed = self.execute(source)
                self.assertTrue(failed,text)
        self.assertFalse(outside.exists())
        for tool in ['read','write','delete']:
            runner=self.registry.nodes['engineer'].toolbox.runner(workspace=self.root)
            args={'targets':[{'path':'.env',**({'content':'x'} if tool=='write' else {})}]}
            text,failed=runner(tool,args)
            self.assertTrue(failed,text)

    def test_only_py_script_paths_and_own_role_skills_are_accepted(self):
        runner=self.registry.nodes['engineer'].toolbox.runner(workspace=self.root)
        for command in ['-c:print(1)','pip install x',
                        'script.ps1',
                        'python skill://literature-search/scripts/research_web.py --input x.json']:
            text,failed=runner('run_code',{'targets':[{'file_path':command}]})
            self.assertTrue(failed,text)

    def test_child_python_inherits_permissions(self):
        (self.root/'child.py').write_text("open('child.txt','w').write('ok')",encoding='utf-8')
        source="import subprocess,sys; subprocess.run([sys.executable,'child.py'],check=True)"
        text,failed=self.execute(source)
        self.assertFalse(failed,text)
        self.assertEqual((self.root/'child.txt').read_text(),'ok')
        (self.root/'child.txt').unlink()
        text,failed=self.execute(source,'mentor')
        self.assertTrue(failed,text)
        self.assertFalse((self.root/'child.txt').exists())

    def test_url_dns_redirect_and_host_whitelists(self):
        for url in ['file:///c:/secret','http://127.0.0.1','https://unknown.invalid',
                    'https://user:password@python.org','https://python.org:8443']:
            with self.subTest(url=url),self.assertRaises(PermissionError):
                check_url(url,resolve=False)
        for address in ['127.0.0.1','10.0.0.1','169.254.169.254','::1']:
            with patch('backend.security.policy.socket.getaddrinfo',return_value=[(2,1,6,'',(address,443))]):
                with self.assertRaises(PermissionError):check_url('https://python.org')
        with patch('backend.security.policy.socket.getaddrinfo',return_value=[(2,1,6,'',('1.1.1.1',443))]):
            self.assertEqual(check_url('https://docs.python.org/3/'),'https://docs.python.org/3/')
        with self.assertRaises(PermissionError):
            ApprovedRedirect().redirect_request(None,None,302,'',{},'http://127.0.0.1/')

    def test_project_scripts_have_no_network_or_inherited_secrets(self):
        text,failed=self.execute("print('» α 🧪')")
        self.assertFalse(failed,text)
        self.assertIn('» α 🧪',text)
        with patch.dict('os.environ',{'PEIFENG_FAKE_SECRET':'never_inherit'}):
            text,failed=self.execute("import os; print(os.environ.get('PEIFENG_FAKE_SECRET','removed'))")
            self.assertFalse(failed,text)
            self.assertIn('removed',text)
            self.assertNotIn('never_inherit',text)
        text,failed=self.execute("import urllib.request; urllib.request.urlopen('https://python.org',timeout=1)")
        self.assertTrue(failed,text)

    def test_search_fallback_and_pdf_fetch_preserve_evidence_levels(self):
        from io import BytesIO
        from email.message import Message
        from reportlab.pdfgen.canvas import Canvas
        from backend.utils import web
        class Response(BytesIO):
            url='https://python.org/example.pdf'
            headers=Message()
        Response.headers['Content-Type']='application/pdf'
        buffer=BytesIO();canvas=Canvas(buffer)
        canvas.drawString(50,700,'Actual PDF text');canvas.save()
        with patch.object(web,'urlopen',return_value=Response(buffer.getvalue())):
            result=web.retrieve('https://python.org/example.pdf')
        self.assertEqual(result['source_level'],'extracted_pdf_text')
        self.assertIn('Actual PDF text',result['content'])
        html=b'<a class="result__a" href="https://www.python.org/">Python</a>'
        with patch.object(web,'urlopen',side_effect=[TimeoutError('fixture timeout'),Response(html)]):
            result=web.search_public('python',2)
        self.assertEqual(result[0]['search_source'],'duckduckgo')
        self.assertIn('fixture timeout',result[0]['retrieval_warnings'][0])


if __name__=='__main__':
    unittest.main()
