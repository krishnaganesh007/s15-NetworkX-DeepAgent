import asyncio
import sys
import shutil
import json
import os
from pathlib import Path
from contextlib import AsyncExitStack
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.types import Tool
from rich import print

class MultiMCP:
    def __init__(self, config_path="mcp_config.json"):
        self.exit_stack = AsyncExitStack()
        self.sessions = {}  # server_name -> session
        self.tools = {}     # server_name -> [Tool]
        self.config_path = config_path
        self.server_configs = self._load_config()

    def _load_config(self):
        """Load configuration from JSON"""
        path = Path(self.config_path)
        if not path.exists():
            # Fallback to hardcoded for safety if file missing in dev
            print(f"[bold red]⚠️ Config file {self.config_path} not found. using defaults.[/bold red]")
            return {
                "browser": {"command": "uv", "args": ["run", "mcp_servers/server_browser.py"]},
                "rag": {"command": "uv", "args": ["run", "mcp_servers/server_rag.py"]},
                "sandbox": {"command": "uv", "args": ["run", "mcp_servers/server_sandbox.py"]}
            }
        
        try:
            with open(path, "r") as f:
                data = json.load(f)
                return data.get("mcpServers", {})
        except Exception as e:
            print(f"[red]Error loading config: {e}[/red]")
            return {}

    async def start(self):
        """Start all configured servers"""
        print(f"[bold green]🚀 Starting MCP Servers from {self.config_path}...[/bold green]")
        
        for name, config in self.server_configs.items():
            try:
                # 1. Handle command resolution
                cmd = config.get("command", "uv")
                args = config.get("args", [])
                
                # Check absolute vs relative path for scripts
                # If using 'uv run', ensures paths are correct
                if cmd == "uv" and not shutil.which("uv"):
                    cmd = sys.executable
                    # Attempt to fixup args if converting uv -> python
                    if args and args[0] == "run":
                        args = args[1:] if len(args) > 1 else []
                
                # 2. Prepare Parameters
                server_params = StdioServerParameters(
                    command=cmd,
                    args=args,
                    env=None # Could load from config["env"] if needed
                )
                
                # 3. Connect with Timeout Protection for Initialization
                try:
                    read, write = await self.exit_stack.enter_async_context(stdio_client(server_params))
                    session = await self.exit_stack.enter_async_context(ClientSession(read, write))
                    
                    # Initialize with timeout
                    # S20 Fix: 20s timeout for stability
                    await asyncio.wait_for(session.initialize(), timeout=20.0)
                    
                    # List tools
                    result = await session.list_tools()
                    self.sessions[name] = session
                    self.tools[name] = result.tools
                    
                    print(f"  ✅ [cyan]{name}[/cyan] connected. Tools: {len(result.tools)}")
                    
                except asyncio.TimeoutError:
                    print(f"  ❌ [red]{name}[/red] timed out during initialization (20s limit)")
                
            except Exception as e:
                print(f"  ❌ [red]{name}[/red] failed to start: {e}")

    async def stop(self):
        """Stop all servers"""
        print("[bold yellow]🛑 Stopping MCP Servers...[/bold yellow]")
        await self.exit_stack.aclose()

    def get_all_tools(self) -> list:
        """Get all tools from all connected servers"""
        all_tools = []
        for tools in self.tools.values():
            all_tools.extend(tools)
        return all_tools

    async def function_wrapper(self, tool_name: str, *args):
        """Execute a tool using positional arguments by mapping them to schema keys"""
        # Find tool definition
        target_tool = None
        for tools in self.tools.values():
            for tool in tools:
                if tool.name == tool_name:
                    target_tool = tool
                    break
            if target_tool: break
        
        if not target_tool:
            return f"Error: Tool {tool_name} not found"

        # Map positional args to keyword args based on schema
        arguments = {}
        schema = target_tool.inputSchema
        if schema and 'properties' in schema:
            keys = list(schema['properties'].keys())
            for i, arg in enumerate(args):
                if i < len(keys):
                    arguments[keys[i]] = arg
        
        try:
            result = await self.route_tool_call(tool_name, arguments)
            # Unpack CallToolResult
            if hasattr(result, 'content') and result.content:
                return result.content[0].text
            return str(result)
        except Exception as e:
            return f"Error executing {tool_name}: {str(e)}"

    def get_tools_from_servers(self, server_names: list) -> list:
        """Get flattened list of tools from requested servers"""
        all_tools = []
        for name in server_names:
            if name in self.tools:
                all_tools.extend(self.tools[name])
        return all_tools

    async def call_tool(self, server_name: str, tool_name: str, arguments: dict):
        """Call a tool on a specific server with S20 Timeout Fix"""
        if server_name not in self.sessions:
            raise ValueError(f"Server '{server_name}' not connected")
        
        # S20 Fix: Enforce 20s timeout per tool call
        try:
            return await asyncio.wait_for(
                self.sessions[server_name].call_tool(tool_name, arguments),
                timeout=20.0 # Prevent infinite hangs
            )
        except asyncio.TimeoutError:
            raise TimeoutError(f"Tool '{tool_name}' on server '{server_name}' timed out after 20 seconds.")

    # Helper to route tool call by finding which server has it
    async def route_tool_call(self, tool_name: str, arguments: dict):
        for name, tools in self.tools.items():
            for tool in tools:
                if tool.name == tool_name:
                    return await self.call_tool(name, tool_name, arguments)
        raise ValueError(f"Tool '{tool_name}' not found in any server")
