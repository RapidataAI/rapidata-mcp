from mcp.server.fastmcp import FastMCP
from rapidata import RapidataClient, LabelingSelection, ValidationSelection
import os
from typing import Any, Optional
import logging
import sys
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
    level=logging.INFO,
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

@mcp.tool()
async def get_free_text_responses(
    name: str, 
    instruction: str, 
    total_responses: int = 10,
    paths: Optional[list[str]] = None,
) -> dict[str, Any]:
    """get free text responses from humans

    Will ask actual humans to provide some short free text responses to the question.

    1 response costs roughly 0.01$.
    responses are: total_responses * len(paths)

    Args:
        name (str): The name of the order (will not effect the results but used to identify the order).
        instruction (str): The question asked to the people. They will try to answer is. (example "Who is your favorite actor?")
        total_responses (int): The total number of responses that will be collected. More responses will take SIGNIFICANTLY longer. defaults to 5.
        paths (Optional[list[str]]): links or local paths to to the image/video that will be shown to the people. If not provided, a default image will be used.

    Returns:
        list[dict[str, Any]]: dictionary containing the free text responses for each of the images/videos
    """
    try:
        logger.info(f"get_free_text_responses called with name: {name}, instruction: {instruction}")
        logger.debug(f"Total responses: {total_responses}")
        client = RapidataClient()
        client.order._set_priority(500)

        if paths is not None:
            logger.debug(f"Using provided links: {paths}")
            datapoints = paths
        else:
            datapoints = ["https://assets.rapidata.ai/152c11b5-c428-4489-ad83-1651ebfe0efd.jpeg"]

        order = client.order.create_free_text_order(
            name=name,
            instruction=instruction,
            datapoints=datapoints,
            responses_per_datapoint=total_responses,
        ).run()

        logger.info("Free text order created and run successfully")

        try:
            order.view()
        except Exception as e:
            logger.error(f"Error viewing ranking make sure to update your rapidata version")
        results = order.get_results()["results"]
        weighted_results2 = {result["originalFileName"]: result["aggregatedResults"] for result in results} # doesn't decode
        logger.info("Successfully retrieved free text results")
        return weighted_results2
    except Exception as e:
        logger.error(f"Error in get_free_text_responses: {str(e)}", exc_info=True)
        return {"error": f"Failed to get free text responses: {str(e)}"}

@mcp.tool()
async def classification(
    name: str,
    instruction: str,
    answer_options: list[str],
    paths: Optional[list[str]] = None,
    total_responses: int = 25,
) -> list[dict[str, float]]:
    """get classification responses from humans

    Will ask actual humans to classify the images in the directory.

    One response costs roughly 0.001$. responses are: total_responses * len(paths)

    Args:
        name (str): The name of the order (will not effect the results but used to identify the order).
        instruction (str): The question asked to the people. They will try to select the answer based on the question. (example "What is shown in the image?")
        answer_options (list[str]): The options that will be shown to the people. They will have to choose one of them. (maximum 6 options).
            (example ["cat", "dog", "car", "tree"])
        paths (Optional[list[str]]): links or local paths to the image/video that will be shown to the people. If not provided, a default image will be used.
        total_responses (int): The total number of responses that will be collected. More responses will take longer but give a clearer results. defaults to 25.
            if a directory is provided, this will be the number of responses PER image.

    Returns:
        list[dict[str, float]]: list of dictionaries containing the classification results for each image/video
    """
    try:
        logger.info(f"classification called with name: {name}, instruction: {instruction}")
        logger.debug(f"Answer options: {answer_options}")
        client = RapidataClient()
        client.order._set_priority(500)

        if paths is None:
            full_paths = ["https://assets.rapidata.ai/152c11b5-c428-4489-ad83-1651ebfe0efd.jpeg"]
        else:
            full_paths = paths

        order = client.order.create_classification_order(
            name=name,
            instruction=instruction,
            answer_options=answer_options,
            datapoints=full_paths,
            responses_per_datapoint=total_responses,
            selections=[
                ValidationSelection("66d5ac99fc00255c2926df0c", amount=2),
                LabelingSelection(amount=1)]
        ).run()

        logger.info("Classification order created and run successfully")

        try:
            order.view()
        except Exception as e:
            logger.error(f"Error viewing ranking make sure to update your rapidata version")

        results = order.get_results()["results"]
        weighted_results = {result["originalFileName"]: result["summedUserScoresRatios"] for result in results}
        logger.debug(f"Weighted results: {weighted_results}")
        logger.info("Successfully retrieved classification results")
        return weighted_results
    except Exception as e:
        logger.error(f"Error in classification: {str(e)}", exc_info=True)
        return {"error": f"Failed to get classification results: {str(e)}"}
    
# @mcp.tool()
# async def timestamp(
#     name: str,
#     instruction: str,
#     links: list[str],
#     total_responses: int = 25,
# ) -> list[dict[str, float]]:
#     """get timestamp responses from humans

#     Will ask actual humans to provide timestamps in a video.

#     Args:
#         name (str): The name of the order (will not effect the results but used to identify the order).
#         instruction (str): The question asked to the people. They will try to select the answer based on the question. (example "What is shown in the image?")
#         links (Optional[list[str]]): link to the video that will be shown to the people.
#         total_responses (int): The total number of responses that will be collected. More responses will take longer but give a clearer results. defaults to 25.
#             if a directory is provided, this will be the number of responses PER image.

#     Returns:
#         list[dict[str, float]]: list of dictionaries containing the timestamp results for each image
#     """
#     try:
#         logger.info(f"timestamp called with name: {name}, instruction: {instruction}")
#         client = RapidataClient()
#         client.order._set_priority(200)

#         full_paths = links

#         order = client.order.create_timestamp_order(
#             name=name,
#             instruction=instruction,
#             datapoints=full_paths,
#             responses_per_datapoint=total_responses,
#         ).run()

#         logger.info("timestamp order created and run successfully")

#         try:
#             order.view()
#         except Exception as e:
#             logger.error(f"Error viewing ranking make sure to update your rapidata version")

#         results = order.get_results()["results"]
#         weighted_results = [result["aggregatedResults"] for result in results]
#         logger.debug(f"Weighted results: {weighted_results}")
#         logger.info("Successfully retrieved timestamp results")
#         return weighted_results
#     except Exception as e:
#         logger.error(f"Error in timestamp: {str(e)}", exc_info=True)
#         return {"error": f"Failed to get timestamp results: {str(e)}"}

# @mcp.tool()
# async def rank_images(dir_path: str, 
#                       name: str,
#                       instruction: str,
#                       total_comparison_budget: int = 25,
#                       ) -> dict[str, Any]:
#     """rank images in a local dir based on *human* preference

#     Will ask hundreds of actual humans to rank the images in the directory.

#     Args:
#         dir_path (str): path to the directory containing images
#         name (str): The name of the order (will not effect the results but used to identify the order).
#         instruction (str): The question asked to the people. Based on this they will rank the images. (example "Which image looks better?")
#             There will be pair wise matchups rated by humans. The results will effect the elo score of the images. The question will be shown with 2 images.
#         total_comparison_budget (int): The total number of comparisons to be made. This is the total number of pairwise matchups that will be shown to humans. 
#             The more images there are the more budget is required and the more precise the results will be. But it will also take longer. defaults to 25.

#     Returns:
#         dict[str, Any]: dictionary containing the final elo rankings of the images
#     """
#     logger.info(f"rank_images called with dir_path: {dir_path}")

    
#     try:
#         logger.debug("Initializing RapidataClient")
#         client = RapidataClient()
#         logger.debug("Setting order priority to 200")
#         client.order._set_priority(200)
        
#         # Create base path
#         files = os.listdir(dir_path)
#         paths = [os.path.join(dir_path, f) for f in files]
#         logger.debug(f"Full image paths: {dir_path}")

#         # Create ranking order
#         logger.info("Creating ranking order")
#         try:
#             order = client.order.create_ranking_order(
#                 name=name,
#                 instruction=instruction,
#                 datapoints=paths,
#                 responses_per_comparison=1,
#                 total_comparison_budget=total_comparison_budget,
#             )
#             logger.info(f"Ranking order created successfully: {order}")
#             logger.debug(f"Order details: {vars(order)}")
#         except Exception as e:
#             logger.error(f"Error creating ranking order: {str(e)}", exc_info=True)
#             return {"error": f"Failed to create ranking order: {str(e)}"}
        
#         # Run the order
#         try:
#             logger.info("Running ranking order")
#             order.run()
#             try:
#                 order.view()
#             except Exception as e:
#                 logger.error(f"Error viewing ranking make sure to update your rapidata version")
#             logger.info("Ranking order execution started")
#         except Exception as e:
#             logger.error(f"Error running ranking order: {str(e)}", exc_info=True)
            
#             return {"error": f"Failed to run ranking order: {str(e)}"}
        
#         # Get results
#         try:
#             logger.info("Retrieving ranking results")
#             # while order.get_status() == "Processing":
#             #     progress = order._workflow_progress.completion_percentage
#             #     await ctx.report_progress(progress, 100)
#             #     # ctx.info(f"Ranking order progress: {progress}%")
#             #     logger.info(f"Ranking order progress: {progress}%")
#             #     time.sleep(5)
#             # logger.info("Ranking order completed")
#             results = order.get_results()
#             logger.info(f"results: {results}")
#             results = results["summary"]
#             logger.info("Successfully retrieved ranking results")
#             logger.debug(f"Ranking results: {results}")
            
#             return results
#         except Exception as e:
#             logger.error(f"Error getting ranking results: {str(e)}", exc_info=True)
            
#             return {"error": f"Failed to get ranking results: {str(e)}"}
            
#     except Exception as e:
#         logger.critical(f"Unexpected error in rank_images: {str(e)}", exc_info=True)
        
#         # Restore the original stdout
#         # sys.stdout = original_stdout
#         return {"error": f"Unexpected error: {str(e)}"}

# @mcp.tool()
# async def compare_texts(
#     text_pairs: list[list[str]],
#     name: str, 
#     instruction: str, 
#     total_responses: int = 15,
#     language: str = "en",
# ) -> list[dict[str, int]]:
#     """compare two texts and get human preference

#     Will ask actual humans to compare the two texts and choose the better one.

#     Args:
#         text_pairs (list[list[str]]): list of pairs of texts to be compared. Each pair should be a list of exactly two strings.
#         name (str): The name of the order (will not effect the results but used to identify the order).
#         instruction (str): The question asked to the people. They will try to choose the better text based on this. (example "Which text is do you prefer?")
#         total_responses (int): The total number of responses that will be collected. More responses will take longer but give a clearer results. defaults to 15.
#         language (str): The language of the texts. Has to be given as 2 LOWERCASE letters defaults to "en".

#     Returns:
#         list[dict[str, int]]: list of dictionaries containing the comparison results for each pair of texts
#     """
#     try:
#         logger.info(f"compare_texts called with name: {name}, instruction: {instruction}")
#         logger.debug(f"Total responses: {total_responses}")
#         client = RapidataClient()
#         client.order._set_priority(200)

#         order = client.order.create_compare_order(
#             name=name,
#             instruction=instruction,
#             datapoints=text_pairs,
#             responses_per_datapoint=total_responses,
#             data_type="text",
#             filters=[LanguageFilter(language_codes=[language])],
#         ).run()

#         logger.info("Text comparison order created and run successfully")

#         try:
#             order.view()
#         except Exception as e:
#             logger.error(f"Error viewing ranking make sure to update your rapidata version")
#         results = order.get_results()
#         results = [result["aggregatedResults"] for result in results["results"]]
#         logger.debug(f"Text comparison results: {results}")
#         logger.info("Successfully retrieved text comparison results")
#         return results
#     except Exception as e:
#         logger.error(f"Error in compare_texts: {str(e)}", exc_info=True)
#         return {"error": f"Failed to compare texts: {str(e)}"}

if __name__ == "__main__":
    logger.info("Starting FastMCP server for rapidata")
    
    try:
        logger.info("Running FastMCP server with stdio transport")
        mcp.run(transport='stdio')
    except Exception as e:
        logger.critical(f"Fatal error running MCP server: {str(e)}", exc_info=True)
        print(f"Error running MCP server: {str(e)}")
