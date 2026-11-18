"""测试通过真实run_code入口调用迁移后的文档脚本，不保留旧document工具。"""
import json

def run_document(runner, root, payload):
    mapping={'docx':('document-formatting','create_word','create'),
             'markdown':('document-formatting','create_word','markdown'),
             'xlsx':('spreadsheets','edit_workbook','create'),
             'pdf':('pdf-documents','process_pdf','create'),
             'render':('pdf-documents','process_pdf','render')}
    groups={}
    for item in payload['targets']:
        target=dict(item);kind=target.pop('kind')
        skill,module,action=mapping.get(kind,('pdf-documents','process_pdf',kind))
        target['action']=action
        groups.setdefault((skill,module),[]).append(target)
    internal=root/'internal';internal.mkdir(exist_ok=True)
    commands=[]
    sequence=len(list(internal.glob('document-test-*.json')))
    for i,((skill,module),targets) in enumerate(groups.items(),sequence):
        file=internal/f'document-test-{i}.json'
        file.write_text(json.dumps({'targets':targets},ensure_ascii=False),encoding='utf-8')
        commands.append({'file_path':f'skill://{skill}/scripts/{module}.py','args':['--input',f'internal/{file.name}','--base-dir','.'],'timeout':120})
    return runner('run_code',{'targets':commands})


def run_figure(runner, root, payload):
    folder=root/'internal';folder.mkdir(exist_ok=True)
    path=folder/'figure-test.json'
    path.write_text(json.dumps(payload,ensure_ascii=False),encoding='utf-8')
    return runner('run_code',{'targets':[{"file_path": "skill://research-figures/scripts/draw_figure.py", "args": ["--input", "internal/figure-test.json", "--base-dir", "."],'timeout':120}]})
