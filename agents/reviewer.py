"""Independent, read-only structured review of frozen managed source."""
import json
import os

from dotenv import load_dotenv
from openai import OpenAI, OpenAIError

from agents.prompt_loader import REVIEWER_PROMPT, load_prompt
from agents.requirements import ENV_PATH
from models.design import DesignOutput
from models.planning import ExecutionPlan
from models.requirements import RequirementsOutput
from models.review import CodeReviewResult
from orchestrator.revision import frozen_files


def generate_review(context: dict) -> CodeReviewResult:
    load_dotenv(ENV_PATH)
    key, model = os.getenv('OPENAI_API_KEY', '').strip(), os.getenv('OPENAI_MODEL', '').strip()
    if not key or not model:
        raise ValueError('Review requires OpenAI configuration.')
    prompt = load_prompt(REVIEWER_PROMPT)
    with OpenAI(api_key=key, timeout=90.0, max_retries=0) as client:
        response = client.responses.parse(model=model, text_format=CodeReviewResult,
            input=[{'role':'system','content':prompt}, {'role':'user','content':json.dumps(context,ensure_ascii=False)}])
    if response.status != 'completed' or response.output_parsed is None:
        raise ValueError('Incomplete code review.')
    return CodeReviewResult.model_validate(response.output_parsed)


def validate_review(result: CodeReviewResult, files: dict[str, str], revision: str, story_ids: set[str]) -> CodeReviewResult:
    if result.reviewed_revision != revision:
        raise ValueError('Review revision mismatch.')
    if len(result.reviewed_files) != len(set(result.reviewed_files)) or set(result.reviewed_files) != set(files):
        raise ValueError('Review must cover every frozen file.')
    ids = [f.finding_id for f in result.findings]
    if len(ids) != len(set(ids)):
        raise ValueError('Duplicate finding IDs.')
    for finding in result.findings:
        content = files.get(finding.file_path)
        if content is None or not finding.code_evidence.strip() or finding.code_evidence not in content:
            raise ValueError('Finding evidence must reference frozen source.')
        if not set(finding.related_user_story_ids) <= story_ids:
            raise ValueError('Unknown story reference.')
        if finding.line_number is not None:
            lines = content.splitlines()
            start = finding.line_number - 1
            excerpt = '\n'.join(lines[start:start + len(finding.code_evidence.splitlines())])
            if finding.code_evidence not in excerpt:
                raise ValueError('Finding line number does not match evidence.')
    blocking = sum(f.severity in {'critical','high'} for f in result.findings)
    # Severity is the deterministic policy; never trust a claimed count or summary.
    result.blocking_findings_count = blocking
    if blocking and result.status == 'approved':
        result.status = 'changes_requested'
    return result


def reviewer_agent(state: dict) -> dict:
    revision = state.get('validation_revision') or ''
    try:
        files = frozen_files(state)
        requirements = RequirementsOutput.model_validate(state['requirements'])
        design = DesignOutput.model_validate(state['design'])
        plan = ExecutionPlan.model_validate(state['plan'])
        if requirements.requires_human_clarification:
            raise ValueError('Requirements need clarification.')
        result = generate_review({'requirements':requirements.model_dump(), 'design':design.model_dump(),
            'plan':plan.model_dump(), 'files':files, 'revision':revision})
        result = validate_review(result, files, revision, {s.id for s in requirements.user_stories})
    except (ValueError, TypeError, KeyError, OSError, OpenAIError):
        result = CodeReviewResult(status='error', summary='Review unavailable or invalid evidence, schema, or revision. Check configuration and review contract.',
            findings=[], blocking_findings_count=0, reviewed_files=[], reviewed_revision=revision)
    result.retry_attempt = state.get('qa_repair_count',0)
    return {'review_result':result.model_dump(mode='json')}
