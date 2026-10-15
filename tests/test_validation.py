import json
import threading
from pathlib import Path

import pytest
from langgraph.checkpoint.sqlite import SqliteSaver

from models.qa import QAResult
from models.review import CodeReviewResult
from orchestrator.graph import build_graph
from orchestrator.file_writer import SafeWorkspace
from orchestrator.revision import validation_snapshot
from orchestrator.validation import validation_gate
from tests.conftest import approved_review
from tests.test_qa_recovery import ready, execute, repair, fail, RUN
from tests.test_review import finding


def passed():
    return QAResult(status='qa_passed',exit_code=0,total_tests=1,passed_tests=1)


def review(context, status='approved'):
    result=approved_review(context)
    result.status=status
    if status=='changes_requested':
        result.findings=[finding()]
        result.blocking_findings_count=1
    return result


@pytest.mark.parametrize('qa_status, review_status, expected', [
    ('qa_passed','approved','validation_passed'),
    ('qa_failed','approved','validation_failed'),
    ('qa_passed','changes_requested','validation_failed'),
    ('qa_failed','changes_requested','validation_failed'),
    ('qa_blocked','approved','validation_blocked'),
    ('qa_passed','error','validation_blocked'),
    ('qa_failed','blocked','validation_blocked'),
])
def test_combined_outcomes(ready,tmp_path,monkeypatch,qa_status,review_status,expected):
    ready['qa_repair_count']=2  # Exercise gate policy without automatic repairs.
    q=passed() if qa_status=='qa_passed' else fail() if qa_status=='qa_failed' else QAResult(status='qa_blocked')
    monkeypatch.setattr('agents.qa.run_tests',lambda *a,**k:q)
    monkeypatch.setattr('agents.reviewer.generate_review',lambda context:review(context,review_status))
    result,transitions=execute(ready,tmp_path)
    assert result['status']==expected
    assert transitions.count('validation_gate')==1
    assert result['qa_result']['revision']==result['review_result']['reviewed_revision']
    assert result['validation_result']['final_human_approval'] is False


@pytest.mark.parametrize('change',['qa_revision','review_revision','attempt','missing_qa','missing_review'])
def test_missing_or_stale_result_blocks(ready,change):
    ready.update(validation_snapshot(ready)); revision=ready['validation_revision']
    ready['qa_result']={**passed().model_dump(),'revision':revision}
    ready['review_result']=approved_review({'files':ready['validation_files'],'revision':revision}).model_dump()
    if change=='qa_revision': ready['qa_result']['revision']='stale'
    if change=='review_revision': ready['review_result']['reviewed_revision']='stale'
    if change=='attempt': ready['qa_result']['retry_attempt']=1
    if change=='missing_qa': ready['qa_result']=None
    if change=='missing_review': ready['review_result']=None
    assert validation_gate(ready)['status']=='validation_blocked'


def test_parallel_branches_and_fan_in(ready,tmp_path,monkeypatch):
    barrier=threading.Barrier(2,timeout=5)
    seen=[]
    def qa(*args,**kwargs):
        barrier.wait(); seen.append('qa'); return passed()
    def reviewer(context):
        barrier.wait(); seen.append('review'); return approved_review(context)
    monkeypatch.setattr('agents.qa.run_tests',qa)
    monkeypatch.setattr('agents.reviewer.generate_review',reviewer)
    result,transitions=execute(ready,tmp_path)
    assert sorted(seen)==['qa','review']
    assert result['status']=='validation_passed'
    assert transitions.index('validation_gate')>max(transitions.index('qa'),transitions.index('review'))
    assert result['qa_result'] and result['review_result']  # No concurrent status/history writes.
    print('\nPARALLEL:', ' -> '.join(transitions))


def test_developer_writes_blocked_during_validation(ready):
    ready.update(validation_snapshot(ready))
    with SafeWorkspace(RUN) as workspace:
        with pytest.raises(ValueError,match='blocked during validation'):
            workspace.persist(repair(),'not-allowed')
    ready['qa_result']={**passed().model_dump(),'revision':ready['validation_revision']}
    ready['review_result']=approved_review({'files':ready['validation_files'],'revision':ready['validation_revision']}).model_dump()
    assert validation_gate(ready)['status']=='validation_passed'
    with SafeWorkspace(RUN) as workspace:
        assert 'validation_revision' not in workspace.manifest


@pytest.mark.parametrize('fail_qa',[False,True])
def test_review_feedback_and_combined_repair(ready,tmp_path,monkeypatch,fail_qa):
    qa_calls=[]; review_calls=[]; contexts=[]
    def qa(*args,**kwargs):
        qa_calls.append(1)
        return fail() if fail_qa and len(qa_calls)==1 else passed()
    def reviewer(context):
        review_calls.append(context['revision'])
        return review(context,'changes_requested') if len(review_calls)==1 else approved_review(context)
    def developer(context):
        contexts.append(context)
        return repair()
    monkeypatch.setattr('agents.qa.run_tests',qa)
    monkeypatch.setattr('agents.reviewer.generate_review',reviewer)
    monkeypatch.setattr('orchestrator.qa_recovery.generate_task',developer)
    result,transitions=execute(ready,tmp_path)
    assert result['status']=='validation_passed' and result['qa_repair_count']==1
    assert len(qa_calls)==len(review_calls)==2
    assert review_calls[0]!=review_calls[1]
    assert contexts[0]['review_findings'][0]['finding_id']=='F1'
    if fail_qa:
        assert contexts[0]['qa_feedback']['failing_tests']
    else:
        assert contexts[0]['qa_feedback']['category']=='review_finding'
    print('\nREPAIR:', ' -> '.join(transitions))
    print('HISTORY:',json.dumps(result['recovery_history'],indent=2))


def test_external_edit_during_validation_blocks(ready,monkeypatch,tmp_path):
    from orchestrator import file_writer
    def qa(*a,**k):
        (file_writer.OUTPUT_ROOT/RUN/'application.py').write_text('changed outside harness')
        return passed()
    monkeypatch.setattr('agents.qa.run_tests',qa)
    result,_=execute(ready,tmp_path)
    assert result['status']=='validation_blocked'


def test_checkpoint_reuses_completed_parallel_branch(ready,tmp_path,monkeypatch):
    counts={'qa':0,'review':0}
    def qa(*a,**k): counts['qa']+=1; return passed()
    def reviewer(context):
        counts['review']+=1
        if counts['review']==1: raise RuntimeError('Simulated process interruption')
        return approved_review(context)
    monkeypatch.setattr('agents.qa.run_tests',qa)
    monkeypatch.setattr('agents.reviewer.generate_review',reviewer)
    config={'configurable':{'thread_id':RUN}}
    database=str(tmp_path/'checkpoint.sqlite')
    with SqliteSaver.from_conn_string(database) as saver:
        graph=build_graph(saver); graph.update_state(config,ready,as_node='development_select')
        with pytest.raises(RuntimeError): graph.invoke(None,config)
    with SqliteSaver.from_conn_string(database) as saver:
        result=build_graph(saver).invoke(None,config)
    assert result['status']=='validation_passed'
    assert counts=={'qa':1,'review':2}


def test_gate_rejects_unsupported_qa_success(ready):
    ready.update(validation_snapshot(ready))
    ready['qa_result']={**QAResult(status='qa_passed',exit_code=0).model_dump(),'revision':ready['validation_revision']}
    ready['review_result']=approved_review({'files':ready['validation_files'],'revision':ready['validation_revision']}).model_dump()
    assert validation_gate(ready)['status']=='validation_blocked'


def test_real_parallel_fixture_demo(ready,tmp_path,monkeypatch,capsys):
    """Real pytest against trusted handwritten source; deterministic mocked review/repair."""
    import sys
    import run
    monkeypatch.setattr('orchestrator.qa_runner.qa_python',lambda run_id:Path(sys.executable))
    contexts=[]
    def reviewer(context):
        contexts.append(context)
        return review(context,'changes_requested') if len(contexts)==1 else approved_review(context)
    monkeypatch.setattr('agents.reviewer.generate_review',reviewer)
    monkeypatch.setattr('orchestrator.qa_recovery.generate_task',lambda context:repair())
    monkeypatch.setattr(run,'RUNS_DIR',tmp_path/'runs')
    directory=run.RUNS_DIR/RUN; directory.mkdir(parents=True)
    with SqliteSaver.from_conn_string(str(directory/'checkpoints.sqlite')) as saver:
        build_graph(saver).update_state({'configurable':{'thread_id':RUN}},ready,as_node='development_select')
    assert run.main(['qa','--run-id',RUN,'--authorize-local-execution'])==0
    result=json.loads(capsys.readouterr().out)
    assert result['status']=='validation_passed'
    assert len(contexts)==2 and contexts[0]['revision']!=contexts[1]['revision']
    qa=[h['qa_results'] for h in result['recovery_history'] if h.get('stage')=='qa']
    assert [(q['failed_tests'],q['passed_tests']) for q in qa]==[(1,0),(0,1)]
    for key,name in [('qa_results','qa_results.json'),('review_result','review_results.json'),('validation_result','validation_results.json')]:
        assert json.loads((directory/name).read_text())==result[key]
    print('\nTRUSTED DEMO:',json.dumps({key:result[key] for key in ['status','qa_result','review_result','validation_result','recovery_history']},indent=2))
    print('ARTIFACTS:', 'qa_results.json review_results.json validation_results.json recovery_history.json checkpoints.sqlite')


def test_gate_release_replay_is_idempotent(ready):
    from orchestrator.revision import release_revision
    ready.update(validation_snapshot(ready))
    release_revision(ready)
    # Gate may resume after releasing the barrier but before saving its checkpoint.
    release_revision(ready)


def test_review_test_change_requires_human_approval(ready,tmp_path,monkeypatch):
    monkeypatch.setattr('agents.qa.run_tests',lambda *a,**k:passed())
    def reviewer(context):
        result=review(context,'changes_requested')
        result.findings=[finding(file_path='workspace/output_code/test_app.py',line_number=4,
            code_evidence='assert value() == 1',description='Test changes require human decision.')]
        return result
    monkeypatch.setattr('agents.reviewer.generate_review',reviewer)
    monkeypatch.setattr('orchestrator.qa_recovery.generate_task',lambda c:pytest.fail('No source eligible for repair'))
    result,transitions=execute(ready,tmp_path)
    assert result['status']=='validation_failed'
    assert transitions.count('qa')==1
    assert any('human approval' in event.get('details','') for event in result['recovery_history'])


def test_noop_repair_stops(ready,tmp_path,monkeypatch):
    from tests.test_qa_recovery import BAD
    monkeypatch.setattr('agents.qa.run_tests',lambda *a,**k:fail())
    output=repair(); output.files[0].content=BAD
    monkeypatch.setattr('orchestrator.qa_recovery.generate_task',lambda c:output)
    result,transitions=execute(ready,tmp_path)
    assert result['status']=='validation_failed'
    assert transitions.count('qa')==1
    assert any(event.get('details')=='Repair made no source changes.' for event in result['recovery_history'])


def test_truncated_output_is_not_actionable(ready,tmp_path,monkeypatch):
    result=QAResult(status='qa_failed',exit_code=1,test_output='FAILED test_app.py::test_value',
                    error_summary='Test output exceeded the capture limit; counts are unverified.')
    monkeypatch.setattr('agents.qa.run_tests',lambda *a,**k:result)
    monkeypatch.setattr('orchestrator.qa_recovery.generate_task',lambda c:pytest.fail('Unverified counts cannot authorize a repair'))
    state,transitions=execute(ready,tmp_path)
    assert state['status']=='validation_failed'
    assert not state['validation_result']['actionable']
    assert 'developer_repair' not in transitions
