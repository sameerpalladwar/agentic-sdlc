import json

import httpx
from dotenv import load_dotenv
from openai import OpenAI

import agents.requirements as agent
from orchestrator.graph import build_graph


def test_dotenv_configuration(tmp_path, monkeypatch, llm_response):
    env = tmp_path / ".env"
    env.write_text("OPENAI_API_KEY=dotenv-test-key\nOPENAI_MODEL=dotenv-model\n")
    monkeypatch.delenv("OPENAI_API_KEY")
    monkeypatch.delenv("OPENAI_MODEL")
    monkeypatch.setattr(agent, "ENV_PATH", env)
    monkeypatch.setattr(agent, "load_dotenv", load_dotenv)
    result = agent.requirements_agent({"requirement": "Shorten URLs", "status": "pending"})
    assert result["status"] == "requirements_complete"
    assert agent.OpenAI.call_args.kwargs["api_key"] == "dotenv-test-key"
    assert llm_response.call_args.kwargs["model"] == "dotenv-model"


def test_environment_precedes_dotenv(tmp_path, monkeypatch, llm_response):
    env = tmp_path / ".env"
    env.write_text("OPENAI_API_KEY=dotenv-test-key\nOPENAI_MODEL=dotenv-model\n")
    monkeypatch.setattr(agent, "ENV_PATH", env)
    monkeypatch.setattr(agent, "load_dotenv", load_dotenv)
    assert (
        agent.requirements_agent({"requirement": "Shorten URLs", "status": "pending"})["status"]
        == "requirements_complete"
    )
    assert agent.OpenAI.call_args.kwargs["api_key"] == "test-key"
    assert llm_response.call_args.kwargs["model"] == "test-model"


def test_sdk_structured_parser_with_mock_http(monkeypatch, payload):
    def respond(request):
        body = json.loads(request.content)
        assert body["text"]["format"]["type"] == "json_schema"
        assert body["text"]["format"]["strict"] is True
        return httpx.Response(
            200,
            json={
                "id": "resp_test",
                "object": "response",
                "created_at": 1,
                "model": "test-model",
                "status": "completed",
                "error": None,
                "incomplete_details": None,
                "instructions": None,
                "metadata": {},
                "parallel_tool_calls": False,
                "temperature": 1,
                "top_p": 1,
                "tool_choice": "auto",
                "tools": [],
                "output": [
                    {
                        "id": "msg_test",
                        "type": "message",
                        "role": "assistant",
                        "status": "completed",
                        "content": [
                            {"type": "output_text", "text": json.dumps(payload), "annotations": []}
                        ],
                    }
                ],
            },
        )

    def client(**kwargs):
        return OpenAI(**kwargs, http_client=httpx.Client(transport=httpx.MockTransport(respond)))

    monkeypatch.setattr(agent, "OpenAI", client)
    result = build_graph().invoke({"requirement": "Shorten URLs", "status": "pending"})
    assert result["status"] == "requirements_complete"
    assert result["requirements"] == payload
