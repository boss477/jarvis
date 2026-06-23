"""MCPClient subclass that surfaces tool errors to the LLM instead of swallowing them."""

from loguru import logger
from pipecat.services.mcp_service import MCPClient


class JarvisMCPClient(MCPClient):
    async def _call_tool(self, session, function_name, arguments, result_callback):
        results = None
        error_msg = None
        try:
            results = await session.call_tool(function_name, arguments=arguments)
        except Exception as e:
            error_msg = str(e)
            logger.error(f"Tool '{function_name}' raised: {error_msg}")

        response = ""
        if results:
            if hasattr(results, "content") and results.content:
                for content in results.content:
                    if hasattr(content, "text") and content.text:
                        response += content.text
            else:
                logger.error(f"No content in '{function_name}' result.")

        # Apply output filter if configured
        if function_name in self._tools_output_filters:
            try:
                response = self._tools_output_filters[function_name](response)
            except Exception:
                logger.error(f"Output filter failed for '{function_name}'")
                response = ""

        if not response:
            if error_msg:
                response = f"Tool '{function_name}' failed: {error_msg}"
            else:
                response = f"Tool '{function_name}' returned no output."

        await result_callback(response)
