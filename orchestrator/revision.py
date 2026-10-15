"""Freeze managed source in checkpoint state; block harness writes until fan-in."""
import hashlib
import json

from models.qa import QAResult
from models.review import CodeReviewResult
from orchestrator.design_validation import redact
from orchestrator.file_writer import SafeWorkspace, atomic_write, relative_artifact


def revision_id(files: dict[str, str]) -> str:
    if redact(files) != files:
        raise ValueError("Credential-like content must not be sent to validation.")
    for path, text in files.items():
        relative_artifact(path)
        if '\x00' in text:
            raise ValueError('Binary source is excluded from validation.')
    return hashlib.sha256(json.dumps(files, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def validation_snapshot(state: dict) -> dict:
    try:
        if state['status'] != 'development_complete':
            raise ValueError('Validation requires completed development.')
        with SafeWorkspace(state['run_id']) as workspace:
            if workspace.manifest.get('pending'):
                raise ValueError('Unfinished source persistence.')
            files = workspace.snapshot()
            if not files:
                raise ValueError('No managed source to validate.')
            revision = revision_id(files)
            lease = workspace.manifest.get('validation_revision')
            if lease and lease != revision:
                raise ValueError('Another revision is under validation.')
            workspace.manifest.pop('validation_completed_revision', None)
            workspace.manifest['validation_revision'] = revision
            atomic_write(workspace.manifest_path, json.dumps(workspace.manifest))
        return {'status':'validating', 'validation_revision':revision, 'validation_files':files,
                'qa_result':None, 'qa_results':None, 'review_result':None, 'validation_result':None, 'error':None}
    except (ValueError, TypeError, OSError, KeyError):
        qa = QAResult(status='qa_blocked', error_summary='Unable to freeze managed source.',
                      retry_attempt=state.get('qa_repair_count',0)).model_dump()
        review = CodeReviewResult(status='blocked', summary='No safe frozen source available.', findings=[],
            blocking_findings_count=0, reviewed_files=[], reviewed_revision='',
            retry_attempt=state.get('qa_repair_count',0)).model_dump()
        return {'status':'validation_blocked', 'validation_revision':None, 'validation_files':{},
                'qa_result':qa, 'qa_results':qa, 'review_result':review,
                'validation_result':{'status':'validation_blocked', 'revision':None,
                    'retry_attempt':state.get('qa_repair_count',0), 'reason':'Unable to freeze managed source.'},
                'error':'Unable to freeze managed source.'}


def frozen_files(state: dict) -> dict[str, str]:
    files = state['validation_files']
    if not state.get('validation_revision') or revision_id(files) != state['validation_revision']:
        raise ValueError('Frozen source revision mismatch.')
    return dict(files)


def release_revision(state: dict) -> None:
    with SafeWorkspace(state['run_id']) as workspace:
        revision = state['validation_revision']
        valid = False
        try:
            lease = workspace.manifest.get('validation_revision')
            replay = lease is None and workspace.manifest.get('validation_completed_revision') == revision
            if revision_id(workspace.snapshot()) != revision or not (lease == revision or replay):
                raise ValueError('Source changed while validation was running.')
            valid = True
        finally:
            if workspace.manifest.get('validation_revision') == revision:
                workspace.manifest.pop('validation_revision', None)
                if valid:
                    workspace.manifest['validation_completed_revision'] = revision
                atomic_write(workspace.manifest_path, json.dumps(workspace.manifest))
