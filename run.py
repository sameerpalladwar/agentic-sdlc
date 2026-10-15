from orchestrator.graph import build_graph


def main():
    graph = build_graph()
    initial_state = {
        "requirement": "Build a URL shortener service",
        "requirements_summary": "",
        "status": "pending",
    }
    result = graph.invoke(initial_state)
    print("Requirement:", result["requirement"])
    print("Summary:", result["requirements_summary"])
    print("Status:", result["status"])


if __name__ == "__main__":
    main()
