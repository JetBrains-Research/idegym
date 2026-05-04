from enum import StrEnum


class MCPToolName(StrEnum):
    REGISTER_CLIENT = "register_client"
    STOP_CLIENT = "stop_client"
    FINISH_CLIENT = "finish_client"
    START_SERVER = "start_server"
    STOP_SERVER = "stop_server"
    FINISH_SERVER = "finish_server"
    RESTART_SERVER = "restart_server"
    BUILD_IMAGES_FROM_YAML = "build_images_from_yaml"
    GET_OPERATION_STATUS = "get_operation_status"
    GET_JOB_STATUS = "get_job_status"
    FORWARD_REQUEST = "forward_request"
    RUN_BASH_COMMAND = "run_bash_command"
