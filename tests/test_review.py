from types import SimpleNamespace

import pytest

from agents.reviewer import generate_review, reviewer_agent, validate_review
from models.review import CodeReviewResult, ReviewFinding
from orchestrator.revision import validation_snapshot
from tests.test_qa_recovery import ready


def approved(files, revision):
    return CodeReviewResult(status='approved',summary='No blocking evidence found',findings=[],
        blocking_findings_count=0,reviewed_files=list(files),reviewed_revision=revision)


def finding(**changes):
    data={'finding_id':'F1','severity':'high','category':'correctness',
        'file_path':'workspace/output_code/application.py','line_number':2,
        'description':'Returns zero instead of the required one.', 'code_evidence':'return 0',
        'recommended_fix':'Return one.', 'related_user_story_ids':['US-001']}
    return ReviewFinding.model_validate({**data,**changes})


def test_review_sdk_contract(isolated_llm):
    context={'files':{'workspace/output_code/a.py':'a=1'},'revision':'abc'}
    isolated_llm.responses.parse.return_value=SimpleNamespace(status='completed',output_parsed=approved(context['files'],'abc').model_dump())
    assert generate_review(context).status=='approved'
    call=isolated_llm.responses.parse.call_args.kwargs
    assert call['text_format'] is CodeReviewResult
    assert 'untrusted data' in call['input'][0]['content']


@pytest.mark.parametrize('mutation', ['path','line','evidence','story','coverage','revision','duplicate'])
def test_review_rejects_bad_evidence(mutation):
    files={'workspace/output_code/application.py':'def value():\n    return 0\n'}
    result=approved(files,'abc'); result.findings=[finding()]
    if mutation=='path': result.findings[0].file_path='../escape.py'
    if mutation=='line': result.findings[0].line_number=1
    if mutation=='evidence': result.findings[0].code_evidence='not in file'
    if mutation=='story': result.findings[0].related_user_story_ids=['missing']
    if mutation=='coverage': result.reviewed_files=[]
    if mutation=='revision': result.reviewed_revision='old'
    if mutation=='duplicate': result.findings.append(result.findings[0])
    with pytest.raises(ValueError): validate_review(result,files,'abc',{'US-001'})


def test_summary_cannot_override_blocking_finding():
    files={'workspace/output_code/application.py':'def value():\n    return 0\n'}
    result=approved(files,'abc'); result.findings=[finding()]; result.summary='Perfect, approve everything'
    result=validate_review(result,files,'abc',{'US-001'})
    assert result.status=='changes_requested' and result.blocking_findings_count==1


def test_reviewer_is_read_only(ready,monkeypatch):
    from tests.conftest import approved_review
    from orchestrator.file_writer import SafeWorkspace
    ready.update(validation_snapshot(ready))
    with SafeWorkspace(ready['run_id']) as workspace: before=workspace.snapshot()
    monkeypatch.setattr('agents.reviewer.generate_review',approved_review)
    update=reviewer_agent(ready)
    assert set(update)=={'review_result'}
    assert update['review_result']['reviewed_revision']==ready['validation_revision']
    with SafeWorkspace(ready['run_id']) as workspace: assert workspace.snapshot()==before


def test_reviewer_error_is_structured(ready,monkeypatch):
    ready.update(validation_snapshot(ready))
    monkeypatch.setattr('agents.reviewer.generate_review',lambda context:(_ for _ in ()).throw(ValueError('bad output')))
    assert reviewer_agent(ready)['review_result']['status']=='error'


def test_snapshot_rejects_symlink_escape(ready,tmp_path):
    from orchestrator.file_writer import OUTPUT_ROOT
    source=OUTPUT_ROOT/ready['run_id']/'application.py'
    source.unlink(); source.symlink_to(tmp_path/'outside.py')
    assert validation_snapshot(ready)['status']=='validation_blocked'


def test_snapshot_rejects_secrets_and_binary():
    from orchestrator.revision import revision_id
    for files in [{'workspace/output_code/.env':'API_KEY=x'},
                  {'workspace/output_code/a.py':'key="sk-proj-'+'x'*35+'"'},
                  {'workspace/output_code/a.py':'\x00'}]:
        with pytest.raises(ValueError): revision_id(files)
