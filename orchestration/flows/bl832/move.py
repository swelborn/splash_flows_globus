import datetime
from dotenv import load_dotenv
import os
from pathlib import Path
import time
import uuid

from globus_compute_sdk import Client, Executor
import globus_sdk
from globus_sdk import TransferClient
from prefect import flow, task, get_run_logger
from prefect.blocks.system import JSON
from prefect.blocks.system import Secret

from orchestration.flows.scicat.ingest import ingest_dataset
from orchestration.flows.bl832.config import Config832
from orchestration.globus import GlobusEndpoint, start_transfer
from orchestration.globus_flows_utils import get_flows_client, get_specific_flow_client
from orchestration.prefect import schedule_prefect_flow


# Load environment variables
load_dotenv()

API_KEY = os.getenv("API_KEY")
TOMO_INGESTOR_MODULE =  "orchestration.flows.bl832.ingest_tomo832"


@task(name="transfer_spot_to_data")
def transfer_spot_to_data(
    file_path: str,
    transfer_client: TransferClient,
    spot832: GlobusEndpoint,
    data832: GlobusEndpoint,
):
    logger = get_run_logger()

    # if source_file begins with "/", it will mess up os.path.join
    if file_path[0] == "/":
        file_path = file_path[1:]

    source_path = os.path.join(spot832.root_path, file_path)
    dest_path = os.path.join(data832.root_path, file_path)
    success = start_transfer(
        transfer_client,
        spot832,
        source_path,
        data832,
        dest_path,
        max_wait_seconds=600,
        logger=logger,
    )
    logger.info(f"spot832 to data832 globus task_id: {task}")
    return success


@task(name="transfer_data_to_nersc")
def transfer_data_to_nersc(
    file_path: str,
    transfer_client: TransferClient,
    data832: GlobusEndpoint,
    nersc832: GlobusEndpoint,
):
    logger = get_run_logger()

    # if source_file begins with "/", it will mess up os.path.join
    if file_path[0] == "/":
        file_path = file_path[1:]
    source_path = os.path.join(data832.root_path, file_path)
    dest_path = os.path.join(nersc832.root_path, file_path)

    logger.info(f"Transferring {dest_path} data832 to nersc")

    success = start_transfer(
        transfer_client,
        data832,
        source_path,
        nersc832,
        dest_path,
        max_wait_seconds=600,
        logger=logger,
    )

    return success


@task(name="transfer_data_to_alcf")
def transfer_data_to_alcf(
        file_path: str,
        transfer_client: TransferClient,
        source_endpoint: GlobusEndpoint,
        destination_endpoint: GlobusEndpoint
    ):
    """
    Transfer data to/from ALCF endpoints.
    Args:
        file_path (str): Path to the file that needs to be transferred.
        transfer_client (TransferClient): TransferClient instance.
        source_endpoint (GlobusEndpoint): Source endpoint.
        alcf_endpoint (GlobusEndpoint): Destination endpoint.
    """
    logger = get_run_logger()

    if file_path[0] == "/":
        file_path = file_path[1:]

    source_path = os.path.join(source_endpoint.root_path, file_path)
    dest_path = os.path.join(destination_endpoint.root_path, file_path)

    try:
        success = start_transfer(
            transfer_client,
            source_endpoint,
            source_path,
            destination_endpoint,
            dest_path,
            max_wait_seconds=600,
            logger=logger,
        )
        if success:
            logger.info("Transfer to ALCF completed successfully.")
        else:
            logger.error("Transfer to ALCF failed.")
        return success
    except globus_sdk.services.transfer.errors.TransferAPIError as e:
        logger.error(f"Failed to submit transfer: {e}")
        return False
    

@flow(name="alcf_tomopy_reconstruction_flow")
def alcf_tomopy_reconstruction_flow():
    logger = get_run_logger()
    
    # Initialize the Globus Compute Client
    gcc = Client()
    polaris_endpoint_id = os.getenv("GLOBUS_COMPUTE_ENDPOINT") # COMPUTE endpoint, not TRANSFER endpoint
    gce = Executor(endpoint_id=polaris_endpoint_id, client=gcc)

    reconstruction_func = os.getenv("GLOBUS_RECONSTRUCTION_FUNC")
    source_collection_endpoint = os.getenv("GLOBUS_IRIBETA_CGS_ENDPOINT")
    destination_collection_endpoint = os.getenv("GLOBUS_IRIBETA_CGS_ENDPOINT")
    
    # Define the function inputs
    # Rundir will need to be updated to the correct path for the expiremental data
    function_inputs = {"rundir": "/eagle/IRIBeta/als/sea_shell_test"}

    # Define the json flow
    flow_input = {
        "input": {
        "source": {
            "id": source_collection_endpoint,
            "path": "/sea_shell_test"
        },
        "destination": {
            "id": destination_collection_endpoint,
            "path": "/bl832"
        },
        "recursive_tx": True,
        "compute_endpoint_id": polaris_endpoint_id,
        "compute_function_id": reconstruction_func,
        "compute_function_kwargs": function_inputs
        }
    }
    collection_ids = [flow_input["input"]["source"]["id"], flow_input["input"]["destination"]["id"]]

    # Flow ID (only generate once!)
    flow_id = os.getenv("GLOBUS_FLOW_ID")

    # Run the flow
    fc = get_flows_client()
    flow_client = get_specific_flow_client(flow_id, collection_ids=collection_ids)

    try:
        logger.info("Starting globus flow action")
        flow_action = flow_client.run_flow(flow_input, label="ALS run", tags=["demo", "als", "tomopy"])
        flow_run_id = flow_action['action_id']
        logger.info( flow_action )
        logger.info(f'Flow action started with id: {flow_run_id}')
        logger.info(f"Monitor your flow here: https://app.globus.org/runs/{flow_run_id}")

        # Monitor flow status
        flow_status = flow_action['status']
        logger.info(f'Initial flow status: {flow_status}')
        while flow_status in ['ACTIVE', 'INACTIVE']:
            time.sleep(10)
            flow_action = fc.get_run(flow_run_id)
            flow_status = flow_action['status']
            logger.info(f'Updated flow status: {flow_status}')
            # Log additional details about the flow status
            logger.info(f'Flow action details: {flow_action}')

        if flow_status != 'SUCCEEDED':
            logger.error(f'Flow failed with status: {flow_status}')
            # Log additional details about the failure
            logger.error(f'Flow failure details: {flow_action}')
        else:
            logger.info(f'Flow completed successfully with status: {flow_status}')
    except Exception as e:
        logger.error(f"Error running flow: {e}")


@flow(name="new_832_file_flow")
def process_new_832_file(file_path: str, is_export_control=False, send_to_nersc=True, send_to_alcf=False):
    """
    Sends a file along a path:
        - Copy from spot832 to data832
        - Copy from data832 to NERSC
        - Copy from NERSC to ALCF (if send_to_alcf is True), compute tomography, and copy back to NERSC
        - Ingest into SciCat
        - Schedule a job to delete from spot832 in the future
        - Schedule a job to delete from data832 in the future

    The is_export_control and send_to_nersc flags are functionally identical, but
    they are separate options at the beamlines, so we leave them as separate parameters
    in case the desired behavior changes in the future.

    :param file_path: path to file on spot832
    :param is_export_control: if True, do not send to NERSC ingest into SciCat
    :param send_to_nersc: if True, send to NERSC and ingest into SciCat
    """

    logger = get_run_logger()
    logger.info("starting flow")
    config = Config832()

    # paths come in from the app on spot832 as /global/raw/...
    # remove 'global' so that all paths start with 'raw', which is common
    # to all 3 systems.
    logger.info(f"Transferring {file_path} from spot to data")
    relative_path = file_path.split("/global")[1]
    transfer_spot_to_data(relative_path, config.tc, config.spot832, config.data832)

    logger.info(f"Transferring {file_path} to spot to data")


    # Send data from NERSC to ALCF (default is False), process it using Tomopy, and send it back to NERSC
    if not is_export_control and send_to_alcf:
        # assume file_path is the name of the file without the extension, but it is an h5 file
        # fp adds the .h5 extension back to the string (for the initial transfer to ALCF)
        # ex: file_path = '20230224_132553_sea_shell'
        fp = file_path + '.h5'
        
        # Transfer data from NERSC to ALCF
        logger.info(f"Transferring {file_path} from NERSC to ALCF")
        transfer_success = transfer_data_to_alcf(fp, config.tc, config.nersc_alsdev, config.alcf_iribeta_cgs)
        if not transfer_success:
            logger.error("Transfer failed due to configuration or authorization issues.")
        else:
            logger.info("Transfer successful.")

        logger.info(f"Running Tomopy reconstruction on {file_path} at ALCF")

        # Run the Tomopy reconstruction flow
        alcf_tomopy_reconstruction_flow()

        # Send reconstructed data to NERSC
        logger.info(f"Transferring {file_path} from ALCF to NERSC")
        file_path = '/bl832/rec' + file_path + '/'
        transfer_success = transfer_data_to_nersc(file_path, config.tc, config.alcf_iribeta_cgs, config.nersc_alsdev)
        if not transfer_success:
            logger.error("Transfer failed due to configuration or authorization issues.")
        else:
            logger.info("Transfer successful.")


    if not is_export_control and send_to_nersc:
        transfer_data_to_nersc(
            relative_path, config.tc, config.data832, config.nersc832
        )
        logger.info(
            f"File successfully transferred from data832 to NERSC {file_path}. Task {task}"
        )
        flow_name = f"ingest scicat: {Path(file_path).name}"
        logger.info(f"Ingesting {file_path} with {TOMO_INGESTOR_MODULE}")
        try:
            ingest_dataset(file_path, TOMO_INGESTOR_MODULE)
        except Exception as e:
            logger.error(f"SciCat ingest failed with {e}")
    
        # schedule_prefect_flow(
        #     "ingest_scicat/ingest_scicat",
        #     flow_name,
        #     {"relative_path": relative_path},
        #     datetime.timedelta(0.0),
        # )

    bl832_settings = JSON.load("bl832-settings").value

    flow_name = f"delete spot832: {Path(file_path).name}"
    schedule_spot832_delete_days = bl832_settings["delete_spot832_files_after_days"]
    schedule_data832_delete_days = bl832_settings["delete_data832_files_after_days"]
    schedule_prefect_flow(
        "prune_spot832/prune_spot832",
        flow_name,
        {"relative_path": relative_path},
        datetime.timedelta(days=schedule_spot832_delete_days),
    )
    logger.info(
        f"Scheduled delete from spot832 at {datetime.timedelta(days=schedule_spot832_delete_days)}"
    )

    flow_name = f"delete data832: {Path(file_path).name}"
    schedule_prefect_flow(
        "prune_data832/prune_data832",
        flow_name,
        {"relative_path": relative_path},
        datetime.timedelta(days=schedule_data832_delete_days),
    )
    logger.info(
        f"Scheduled delete from data832 at {datetime.timedelta(days=schedule_data832_delete_days)}"
    )
    return


@flow(name="test_832_transfers")
def test_transfers_832(file_path: str = "/raw/transfer_tests/test.txt"):
    logger = get_run_logger()
    config = Config832()
    # test_scicat(config)
    logger.info(f"{str(uuid.uuid4())}{file_path}")
    # copy file to a uniquely-named file in the same folder
    file = Path(file_path)
    new_file = str(file.with_name(f"test_{str(uuid.uuid4())}.txt"))
    logger.info(new_file)
    success = start_transfer(
        config.tc, config.spot832, file_path, config.spot832, new_file, logger=logger
    )
    logger.info(success)
    spot832_path = transfer_spot_to_data(
        new_file, config.tc, config.spot832, config.data832
    )
    logger.info(f"Transferred {spot832_path} to spot to data")

    task = transfer_data_to_nersc(new_file, config.tc, config.data832, config.nersc832)
    logger.info(
        f"File successfully transferred from data832 to NERSC {spot832_path}. Task {task}"
    )
    process_new_832_file(file_path, is_export_control=False, send_to_nersc=False, send_to_alcf=True)


test_transfers_832('20230224_132553_sea_shell')