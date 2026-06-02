from src.agent.graph import build_agent, build_system_prompt, extract_final_answer, extract_tool_calls, extract_saved_order, run_agent
from pathlib import Path
import json
from typing import Any


# main
if __name__ == "__main__":
    # Example usage:
    print("Running agent with example query...")
    history = []
    query = "Tạo đơn giúp tôi 2 màn hình Dell UltraSharp U2724D và 1 Logitech MX Keys S cho công ty mới."
    while query != "exit":    
        result = run_agent(
            query,
            provider="openai",
            today="2026-06-01",
            output_dir=Path("output"),
        )
        history.append({
            "query": query,
            "your_answer": result.final_answer,
        })
        print("Final Answer:", result.final_answer)
        print("Tool Calls:")
        for call in result.tool_calls:
            print(f"  - {call.name} with args {call.args} returned {call.output}")
        query = input("Enter your next query (or 'exit' to quit): ")
        query = "\n".join(history[-3:]) + "\n" + query  # Include last 3 interactions in the context
