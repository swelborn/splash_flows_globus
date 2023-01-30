import dbm

from prefect import flow, get_run_logger, task


from orchestration.globus import (
    build_apps,
    build_endpoints,
    get_files,
    get_globus_file_object,
    is_globus_file_older,
    GlobusEndpoint,
    init_transfer_client,
    prune_files
)


@flow
def prune_one_data832_file(file: str, if_older_than_days: int = 14):
    logger = get_run_logger()
    context = Context832()
    # get data832 object so we can check age
    data_832_file_obj = get_globus_file_object(context.tc, context.data832, file).result()
    logger.info(f"file object found on data832")
    assert data_832_file_obj is not None, "file not found on data832"
    if not is_globus_file_older(data_832_file_obj, if_older_than_days):
        logger.info(f"Will not prune, file date {data_832_file_obj['last_modified']} is "
                    f"newer than {if_older_than_days} days")
        return
    
    # check that file exists at NERSC
    nersc_832_file_obj = get_globus_file_object(context.tc, context.nersc, file).result()
    logger.info(f"file object found on nersc")
    assert nersc_832_file_obj is not None, "file not found on nersc" 
    
    prune_files(context.tc, context.data832, [file])



@flow(name="prune_many_data832")
def prune_many_data832(project_dir_filter: str, older_than_days: int = 30, test: bool = False):
    logger = get_run_logger()
    context = Context832()
    data_832_project_dirs = context.tc.operation_ls(context.data832.uuid, context.data832.root_path)
    for project_dir in data_832_project_dirs:
        if project_dir['name'] == project_dir_filter:
            found_project = project_dir
            break
    assert found_project is not None, f"Project dir {project_dir_filter} not found."
    
    logger.info(f"found project dir {project_dir_filter}")
    data832_project_dir_path = context.data832.full_path(project_dir['name'])
    nersc_project_dir_path = context.nersc.full_path(project_dir['name'])
    # produce a list of fiels that we know are in both data832 and NERSC. These 
    # can be safely deleted.
    prunable_files = []
    logger.info("getting files from data832")
    data832_files_future =  get_files(context.tc, context.data832, data832_project_dir_path, [], older_than_days)
    logger.info("getting files from nersc")
    nersc_files_future =  get_files(context.tc, context.nersc, nersc_project_dir_path, [], older_than_days)
    
    data832_files = data832_files_future.result()
    nersc_files = nersc_files_future.result()
    logger.info(f"Found files. data832: {len(data832_files)}  nersc: {len(nersc_files)}")
    for data832_file in data832_files:
        nersc_file_analog = context.nersc.root_path + data832_file.split(context.data832.root_path)[1]
        if nersc_file_analog in nersc_files:
            prunable_files.append(data832_file)
    logger.info(f"pruning {len(prunable_files)} files")
    if len(prunable_files) > 0 and not test:
        prune_files(context.tc, context.data832, prunable_files)
         

class Context832:
    def __init__(self) -> None:
        config = get_config()
        self.endpoints = build_endpoints(config)
        self.apps = build_apps(config)
        self.tc = init_transfer_client(self.apps['als_transfer'])

        self.data832 = self.endpoints['data832']
        self.nersc = self.endpoints['nersc']

if __name__ == "__main__":
    prune_many_data832("mihme", test=False, older_than_days=0)