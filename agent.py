import os
import asyncio
from dotenv import load_dotenv
from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_core.messages import ToolMessage, AIMessage, SystemMessage
from langchain_core.tools import tool
from langgraph.prebuilt import create_react_agent
from langgraph.checkpoint.memory import MemorySaver
from langchain_mcp_adapters.client import MultiServerMCPClient

load_dotenv()

import warnings
import logging

warnings.filterwarnings('ignore', message='.*not supported in schema.*')
logging.getLogger().setLevel(logging.ERROR)

# 1. SETUP SERVERS (Standard Config)
mcp_servers = {
    "firecrawl-mcp": {
        "command": "npx.cmd",
        "args": ["-y", "firecrawl-mcp"],
        "env": {"FIRECRAWL_API_KEY": os.getenv("FIRECRAWL_API_KEY")},
        "transport": "stdio",
    }
}

# 2. DEFINE CUSTOM CALCULATOR TOOL (DYNAMIC)
@tool
def calculator(expression: str) -> str:
    """
    Evaluates a mathematical expression and returns the result.
    Use this when you need to perform calculations or solve math problems.
    
    Args:
        expression: A mathematical expression as a string (e.g., "2 + 2", "10 * 5 + 3", "(15 - 3) * 2")
    
    Returns:
        The result of the calculation as a string
    
    Examples:
        - calculator("2 + 2") -> "4"
        - calculator("10 * 5 + 3") -> "53"
        - calculator("(100 - 25) / 5") -> "15.0"
    """
    try:
        # Safe evaluation with limited namespace (no access to dangerous functions)
        allowed_names = {
            "abs": abs, 
            "round": round, 
            "min": min, 
            "max": max,
            "sum": sum, 
            "pow": pow,
            "__builtins__": {}
        }
        result = eval(expression, {"__builtins__": {}}, allowed_names)
        return f"Result: {result}"
    except Exception as e:
        return f"Error calculating '{expression}': {str(e)}. Please check the expression syntax."


# 3. SYSTEM INSTRUCTION
SYSTEM_PROMPT = """You are a helpful AI assistant with access to multiple tools.

IMPORTANT TOOL USAGE RULES:
1. When using 'firecrawl_search':
   - The 'sources' argument must be a LIST OF OBJECTS, not strings.
   - CORRECT: sources=[{"type": "web"}]
   - WRONG: sources=["web"]
   - Always set 'limit' to 1 or 2 to prevent timeouts.

2. When using 'firecrawl_scrape':
   - Ensure you provide a valid URL.

3. When using 'calculator':
   - Provide a valid mathematical expression as a string.
   - You can use operators: +, -, *, /, //, %, **, ()
   - You can use functions: abs, round, min, max, sum, pow
   - Example: "2 + 2", "(10 * 5) / 2", "pow(2, 3)"

Always choose the most appropriate tool for the user's query.
"""

async def run_agent_workflow():
    print("Connecting to MCP Servers...")
    client = MultiServerMCPClient(mcp_servers)
    try:
        mcp_tools = await client.get_tools()
        print(f"Loaded {len(mcp_tools)} MCP Tools")
    except Exception as e:
        print(f"MCP Connection Failed: {e}")
        return

    if not os.getenv("GOOGLE_API_KEY"):
        print("Error: GOOGLE_API_KEY not found.")
        return

    # 4. COMBINE MCP TOOLS + CUSTOM TOOLS
    all_tools = mcp_tools + [calculator]
    print(f"Total Tools Available: {len(all_tools)}")
    print(f"  - MCP Tools: {[t.name for t in mcp_tools]}")
    print(f"  - Custom Tools: [calculator]")

    # 5. SETUP MODEL
    llm = ChatGoogleGenerativeAI(model="gemini-2.5-flash")
    memory = MemorySaver()

    # 6. DEFINE AGENT WITH ALL TOOLS
    agent = create_react_agent(
        llm, 
        all_tools,  # Pass combined tools list
        checkpointer=memory,
        prompt=SYSTEM_PROMPT, 
        interrupt_before=["tools"]  # This will catch ALL tool calls dynamically
    )

    # 7. USER APPROVAL PROMPT - WORKS FOR ALL TOOLS DYNAMICALLY
    def get_user_approval(snapshot):
        last_msg = snapshot.values["messages"][-1]
        
        if not isinstance(last_msg, AIMessage) or not last_msg.tool_calls:
            return None, None
            
        tool_call = last_msg.tool_calls[0]

        print("\n" + "╔" + "═"*60)
        print("║ SECURITY INTERRUPT: TOOL APPROVAL REQUIRED")
        print("╠" + "═"*60)
        print(f"║ TOOL:       {tool_call['name']}")
        print(f"║ ARGUMENTS:  {tool_call['args']}")
        print("╚" + "═"*60)

        response = input("\nApprove this action? (y/n/q): ").lower().strip()
        return response, tool_call

    # 8. MAIN MULTI-TURN LOOP
    print("\nInteractive Session Started")
    print("=" * 60)
    
    config = {"configurable": {"thread_id": "session_v3"}}
    
    # OUTER LOOP: Multi-turn conversation
    while True:
        # Get user input
        user_query = input("\nYou: ").strip()
        
        if user_query.lower() in ['q', 'quit', 'exit']:
            print("Exiting session. Goodbye!")
            break
        
        if not user_query:
            print("Please enter a valid query.")
            continue
        
        current_input = {"messages": [("user", user_query)]}
        
        # INNER LOOP: Handle single query with potential interrupts
        while True:
            try:
                # A. STREAM EXECUTION
                async for event in agent.astream(current_input, config=config):
                    pass

                # B. CHECK STATE
                snapshot = agent.get_state(config)

                if not snapshot.next:
                    # Query completed - show final answer
                    final_msg = snapshot.values["messages"][-1]
                    
                    # Extract clean text from content (handles both string and list formats)
                    if isinstance(final_msg.content, list):
                        # Content is a list of blocks - extract text from each
                        text = " ".join(
                            block.get("text", "") 
                            for block in final_msg.content 
                            if block.get("type") == "text"
                        )
                    else:
                        # Content is already a string
                        text = final_msg.content
                    
                    print(f"\nAgent: {text}")
                    break  # Exit inner loop, go back to get new query

                # C. HANDLE INTERRUPT (WORKS FOR ALL TOOLS DYNAMICALLY)
                if snapshot.next[0] == "tools":
                    decision, tool_call = get_user_approval(snapshot)

                    if decision == 'y':
                        print("Approved. Executing tool...")
                        current_input = None 
                    elif decision == 'q':
                        print("Exiting session.")
                        if hasattr(client, 'close'):
                            await client.close()
                        return
                    else:
                        print("Rejected. Tool call blocked.")
                        rejection_msg = ToolMessage(
                            tool_call_id=tool_call['id'],
                            content=f"User rejected the call to {tool_call['name']}. Please try a different approach or ask the user for clarification."
                        )
                        agent.update_state(config, {"messages": [rejection_msg]}, as_node="tools")
                        current_input = None 

            except KeyboardInterrupt:
                print("\nInterrupted by user. Returning to main menu...")
                break
            except Exception as e:
                if "validation failed" in str(e).lower():
                    print(f"\nSCHEMA ERROR: The model failed to follow instructions.")
                    print(f"   Raw Error: {e}")
                else:
                    print(f"Error: {e}")
                break

    # Cleanup
    if hasattr(client, 'close'):
        await client.close()
    print("Session ended successfully.")

if __name__ == "__main__":
    asyncio.run(run_agent_workflow())