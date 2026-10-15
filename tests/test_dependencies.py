import pytest
from orchestrator.dependencies import FASTAPI_PINS, validate_dependencies


def application():
    return {'workspace/output_code/requirements.txt': '\n'.join(f'{n}=={v}' for n,v in FASTAPI_PINS.items()),
            'workspace/output_code/README.md': 'Target: Python 3.12',
            'workspace/output_code/app/main.py': 'import fastapi\nimport sqlite3\nfrom app import storage\n',
            'workspace/output_code/app/storage.py': 'import sqlite3\n'}


def test_supported_profile():
    assert validate_dependencies(application(), final=True) == []


@pytest.mark.parametrize('line,code', [('sqlite3','stdlib_dependency'), ('json==1.0','stdlib_dependency'),
    ('httpx>=0.24,<0.25','invalid_dependency'), ('httpx==0.24.1','incompatible_dependency'),
    ('-r other.txt','invalid_dependency'), ('httpx @ https://example.com/x','invalid_dependency')])
def test_invalid_dependencies(line, code):
    files=application()
    files['workspace/output_code/requirements.txt']=files['workspace/output_code/requirements.txt'].replace('httpx==0.28.1', '')+'\n'+line
    assert code in {i['code'] for i in validate_dependencies(files,final=True)}


def test_missing_import():
    files=application()
    files['workspace/output_code/app/storage.py']='import sqlalchemy\n'
    assert any('sqlalchemy' in i['message'] for i in validate_dependencies(files,final=True))


@pytest.mark.parametrize('version', ['3.14', '3.13', '3.11'])
def test_unsupported_runtime(version):
    files=application()
    files['workspace/output_code/README.md']='Python '+version
    assert 'unsupported_python' in {i['code'] for i in validate_dependencies(files,final=True)}


def test_missing_requirements():
    files=application(); del files['workspace/output_code/requirements.txt']
    assert 'missing_dependency' in {i['code'] for i in validate_dependencies(files,final=True)}


def test_development_cannot_complete_with_invalid_dependencies(tmp_path, monkeypatch, payload, design_payload):
    from tests.test_qa import completed_state
    from orchestrator import file_writer
    from orchestrator.development import select_task
    monkeypatch.setattr(file_writer,'OUTPUT_ROOT',tmp_path/'output')
    state=completed_state(payload,design_payload,'a'*32)
    monkeypatch.setattr('orchestrator.development.validate_dependencies',lambda *a,**k:[{'code':'missing_dependency','field':'requirements.txt','message':'missing httpx'}])
    result=select_task(state)
    assert result['status']=='failed'
    assert result['development_issues'][0]['code']=='missing_dependency'


def test_invalid_task_not_persisted(tmp_path, monkeypatch, payload, design_payload):
    from tests.test_developer import initial, output
    from orchestrator import file_writer
    from orchestrator.development import prepare_development, select_task, developer_agent
    from models.developer import DeveloperOutput
    monkeypatch.setattr(file_writer, 'OUTPUT_ROOT', tmp_path/'output')
    plan={'tasks':[{'task_id':'T1','title':'Dependencies','description':'Configure packages',
          'related_user_story_ids':['US-001'],'dependencies':[],
          'expected_artifacts':['workspace/output_code/requirements.txt'],
          'acceptance_checks':['Consistent dependencies'],'status':'pending'}], 'shared_artifacts':[]}
    state=initial(payload,design_payload,plan)
    state.update(prepare_development(state)); state.update(select_task(state))
    bad=DeveloperOutput.model_validate(output(state['plan']['tasks'][0], 'sqlite3\n'))
    monkeypatch.setattr('orchestrator.development.generate_task',lambda context:bad)
    result=developer_agent(state)
    assert result['plan']['tasks'][0]['status']=='failed'
    assert result['development_results'][0]['issues'][0]['code']=='stdlib_dependency'
    assert not (file_writer.OUTPUT_ROOT/state['run_id']/'requirements.txt').exists()


def test_qa_rejects_wrong_interpreter_version(tmp_path, monkeypatch):
    from orchestrator import file_writer
    from orchestrator.qa_runner import qa_python
    monkeypatch.setattr(file_writer,'OUTPUT_ROOT',tmp_path/'output')
    root=tmp_path/'qa_venvs'/('a'*32)
    (root/'bin').mkdir(parents=True)
    python=root/'bin/python'; python.write_text(''); python.chmod(0o700)
    (root/'pyvenv.cfg').write_text('version = 3.14.6\ninclude-system-site-packages = false\n')
    monkeypatch.delenv('QA_LOCAL_PYTHON', raising=False)
    with pytest.raises(ValueError,match='Python 3.12'):
        qa_python('a'*32)
