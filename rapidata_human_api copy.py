from mcp.server.fastmcp import FastMCP
from rapidata import RapidataClient
import os
from typing import Any
import logging
import sys
import time
# Create a dummy object that ignores the output
class NullWriter:
    def write(self, msg):
        pass

# Save the original stdout
original_stdout = sys.stdout

# Redirect stdout to the NullWriter
sys.stdout = NullWriter()

# Restore the original stdout
sys.stdout = original_stdout

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
mcp = FastMCP("rapidata")

# Try to set MCP logging if available
try:
    mcp.set_log_level("DEBUG")
    logger.info("Set FastMCP log level to DEBUG")
except (AttributeError, TypeError) as e:
    logger.warning(f"Could not set FastMCP log level: {str(e)}")


@mcp.tool()
async def rank_images(dir_path: str) -> dict[str, Any]:
    """rank images in a local dir based on human preference

    Args:
        dir_path (str): path to the directory containing images
    """
    # Save the original stdout
    original_stdout = sys.stdout

    # Redirect stdout to the NullWriter
    # sys.stdout = NullWriter()

    logger.info(f"rank_images called with dir_path: {dir_path}")
    
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
            
            # Restore the original stdout
            # sys.stdout = original_stdout
            return {"error": f"Directory not found: {base_path}"}
        except PermissionError:
            logger.error(f"Permission denied when accessing directory: {base_path}")
            
            # Restore the original stdout
            # sys.stdout = original_stdout
            return {"error": f"Permission denied when accessing directory: {base_path}"}
        
        # Create full paths
        paths = [base_path + path for path in file_list]
        logger.debug(f"Full image paths: {paths}")
        
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
            # Restore the original stdout
            # sys.stdout = original_stdout
            return {"error": f"Failed to create ranking order: {str(e)}"}
        
        # Run the order
        try:
            logger.info("Running ranking order")
            order.run()
            logger.info("Ranking order execution started")
        except Exception as e:
            logger.error(f"Error running ranking order: {str(e)}", exc_info=True)
            
            # Restore the original stdout
            # sys.stdout = original_stdout
            return {"error": f"Failed to run ranking order: {str(e)}"}
        
        # Get results
        try:
            logger.info("Retrieving ranking results")
            results = order.get_results()["summary"]
            logger.info("Successfully retrieved ranking results")
            logger.debug(f"Ranking results: {results}")
            
            # Restore the original stdout
            # sys.stdout = original_stdout
            return results
        except Exception as e:
            logger.error(f"Error getting ranking results: {str(e)}", exc_info=True)
            
            # Restore the original stdout
            # sys.stdout = original_stdout
            return {"error": f"Failed to get ranking results: {str(e)}"}
            
    except Exception as e:
        logger.critical(f"Unexpected error in rank_images: {str(e)}", exc_info=True)
        
        # Restore the original stdout
        # sys.stdout = original_stdout
        return {"error": f"Unexpected error: {str(e)}"}
    

if __name__ == "__main__":
    logger.info("Starting FastMCP server for rapidata")
    
    try:
        logger.info("Running FastMCP server with stdio transport")
        mcp.run(transport='stdio')
    except Exception as e:
        logger.critical(f"Fatal error running MCP server: {str(e)}", exc_info=True)
        print(f"Error running MCP server: {str(e)}")
