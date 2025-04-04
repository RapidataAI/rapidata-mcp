from mcp.server.fastmcp import FastMCP, Context
from rapidata import RapidataClient
import os
import sys
import time
import logging
import asyncio
from datetime import datetime

# Configure logging
logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler("rapidata_mcp.log"),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# Initialize FastMCP server
logger.info("Initializing FastMCP server with name 'rapidata'")
mcp = FastMCP("rapidata", port=8166)

# Try to set MCP logging if available
try:
    mcp.set_log_level("DEBUG")
    logger.info("Set FastMCP log level to DEBUG")
except (AttributeError, TypeError) as e:
    logger.warning(f"Could not set FastMCP log level: {str(e)}")


# Create a dummy object that ignores the output
class NullWriter:
    def write(self, msg):
        pass


@mcp.tool()
async def rank_images(dir_path: str, ctx: Context) -> dict:
    """rank images in a local dir based on human preference

    Args:
        dir_path (str): path to the directory containing images
    """
    # Get the current request context to extract progress token
    request_context = ctx
    progress_token = None

    mcp_context = mcp.get_context()
    logger.info(f"mcp_context: {mcp_context}")
    # Log the raw context for debugging
    logger.info(f"Received context: {ctx}")

    # Try to deserialize context if possible
    if ctx:
        try:
            logger.debug(f"Context type: {type(ctx)}")
            if hasattr(ctx, '__dict__'):
                logger.debug(f"Context attributes: {ctx.__dict__}")
            else:
                logger.debug("Context has no __dict__ attribute")
        except Exception as e:
            logger.error(f"Error deserializing context: {str(e)}")
    
    # Extract progress token from request context if available
    if ctx and hasattr(request_context, 'params'):
        if hasattr(request_context.params, '_meta'):
            progress_token = getattr(ctx.params._meta, 'progressToken', None)
        elif isinstance(request_context.params, dict) and '_meta' in request_context.params:
            meta_data = request_context.params.get('_meta', {})
            if isinstance(meta_data, dict):
                progress_token = meta_data.get('progressToken')
    
    logger.info(f"rank_images called with dir_path: {dir_path}, progress_token: {progress_token}")

    logger.info(f"request_context: {ctx.request_context}")
    progress_token = ctx.request_context.meta.progressToken
    logger.info(f"Extracted progress token: {progress_token}")

    raise NotImplementedError("This function is not implemented yet.")
    
    try:
        logger.debug("Initializing RapidataClient")
        client = RapidataClient()
        logger.debug("Setting order priority to 200")
        client.order._set_priority(200)
        
        # Create base path
        base_path = dir_path + "\\"
        logger.debug(f"Base path set to: {base_path}")
        
        # List image files
        try:
            file_list = os.listdir(base_path)
            logger.info(f"Found {len(file_list)} files in directory")
            logger.debug(f"Files in directory: {file_list}")
        except FileNotFoundError:
            logger.error(f"Directory not found: {base_path}")
            return {"error": f"Directory not found: {base_path}"}
        except PermissionError:
            logger.error(f"Permission denied when accessing directory: {base_path}")
            return {"error": f"Permission denied when accessing directory: {base_path}"}
        
        # Create full paths
        paths = [base_path + path for path in file_list]
        logger.debug(f"Full image paths: {paths}")
        
        # Send first progress notification
        if progress_token:
            try:
                await mcp.notification({
                    "method": "notifications/progress",
                    "params": {
                        "progress": 1,
                        "total": 5,  # We'll use 5 steps: create order, run, and 3 polling stages
                        "progressToken": progress_token,
                        "message": "Creating ranking order..."
                    }
                })
                logger.debug("Sent initial progress notification")
            except Exception as e:
                logger.error(f"Failed to send progress notification: {str(e)}")
        
        # Create ranking order
        logger.info("Creating ranking order")
        try:
            order = client.order.create_ranking_order(
                name="ranking images",
                instruction="Which image looks better?",
                datapoints=paths,
                responses_per_comparison=1,
                total_comparison_budget=40,
            )
            logger.info(f"Ranking order created successfully: {order}")
            logger.debug(f"Order details: {vars(order)}")
        except Exception as e:
            logger.error(f"Error creating ranking order: {str(e)}", exc_info=True)
            return {"error": f"Failed to create ranking order: {str(e)}"}
        
        # Send second progress notification
        if progress_token:
            try:
                await mcp.notification({
                    "method": "notifications/progress",
                    "params": {
                        "progress": 2,
                        "total": 5,
                        "progressToken": progress_token,
                        "message": "Starting ranking process..."
                    }
                })
                logger.debug("Sent second progress notification")
            except Exception as e:
                logger.error(f"Failed to send progress notification: {str(e)}")
        
        # Run the order
        try:
            logger.info("Running ranking order")
            order.run()
            logger.info("Ranking order execution started")
        except Exception as e:
            logger.error(f"Error running ranking order: {str(e)}", exc_info=True)
            return {"error": f"Failed to run ranking order: {str(e)}"}
        
        # Poll for results with progress notifications
        max_attempts = 30  # Maximum number of polling attempts
        poll_interval = 10  # Seconds between polls
        
        for attempt in range(max_attempts):
            # Calculate progress (steps 3, 4, 5 are for polling)
            progress_step = min(3 + int(attempt * 2 / max_attempts), 5)
            
            # Send polling progress notification
            if progress_token:
                try:
                    await mcp.notification({
                        "method": "notifications/progress",
                        "params": {
                            "progress": progress_step,
                            "total": 5,
                            "progressToken": progress_token,
                            "message": f"Waiting for ranking results... ({attempt + 1}/{max_attempts})"
                        }
                    })
                    logger.debug(f"Sent polling progress notification {attempt + 1}")
                except Exception as e:
                    logger.error(f"Failed to send progress notification: {str(e)}")
            
            # Try to get results
            try:
                logger.info(f"Polling for ranking results (attempt {attempt + 1}/{max_attempts})")
                
                # Check if the order is complete
                # First check if the order has a get_progress method
                if hasattr(order, 'get_progress'):
                    status = order.get_progress()
                    if status.get("status") == "complete":
                        results = order.get_results()["summary"]
                        logger.info("Successfully retrieved ranking results")
                        logger.debug(f"Ranking results: {results}")
                        
                        # Send final progress notification
                        if progress_token:
                            try:
                                await mcp.notification({
                                    "method": "notifications/progress",
                                    "params": {
                                        "progress": 5,
                                        "total": 5,
                                        "progressToken": progress_token,
                                        "message": "Ranking complete!"
                                    }
                                })
                                logger.debug("Sent final progress notification")
                            except Exception as e:
                                logger.error(f"Failed to send progress notification: {str(e)}")
                        
                        return results
                else:
                    # If get_progress is not available, try to get results directly
                    # but catch exceptions if the order is not complete
                    try:
                        results = order.get_results()["summary"]
                        logger.info("Successfully retrieved ranking results")
                        logger.debug(f"Ranking results: {results}")
                        
                        # Send final progress notification
                        if progress_token:
                            try:
                                await mcp.notification({
                                    "method": "notifications/progress",
                                    "params": {
                                        "progress": 5,
                                        "total": 5,
                                        "progressToken": progress_token,
                                        "message": "Ranking complete!"
                                    }
                                })
                                logger.debug("Sent final progress notification")
                            except Exception as e:
                                logger.error(f"Failed to send progress notification: {str(e)}")
                        
                        return results
                    except Exception as e:
                        logger.debug(f"Order not complete yet, will retry: {str(e)}")
                
                await asyncio.sleep(poll_interval)
                
            except Exception as e:
                logger.error(f"Error checking ranking results: {str(e)}", exc_info=True)
                await asyncio.sleep(poll_interval)
        
        # If we've reached here, we've exceeded the maximum attempts
        logger.error("Exceeded maximum polling attempts without getting results")
        return {"error": "Exceeded maximum polling attempts without getting results"}
            
    except Exception as e:
        logger.critical(f"Unexpected error in rank_images: {str(e)}", exc_info=True)
        return {"error": f"Unexpected error: {str(e)}"}


if __name__ == "__main__":
    logger.info("Starting FastMCP server for rapidata")
    
    try:
        logger.info("Running FastMCP server with stdio transport")
        # mcp.run(transport='stdio')
        mcp.run(transport='sse')
    except Exception as e:
        logger.critical(f"Fatal error running MCP server: {str(e)}", exc_info=True)
        print(f"Error running MCP server: {str(e)}")
