from orchestrator.graph import build_graph


def test_requirements_agent_with_valid_requirement():
    result = build_graph().invoke({
        "requirement": "Build a URL shortener service",
        "requirements_summary": "",
        "status": "pending",
    })
    assert result["status"] == "requirements_complete"
    assert "URL shortener" in result["requirements_summary"]


def test_requirements_agent_with_blank_requirement():
    result = build_graph().invoke({
        "requirement": "  ",
        "requirements_summary": "",
        "status": "pending",
    })
    assert result["status"] == "failed"
