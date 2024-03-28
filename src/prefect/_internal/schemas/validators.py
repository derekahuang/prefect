"""

This module contains a collection of functions that are used to validate the
values of fields in Pydantic models. These functions are used as validators in
Pydantic models to ensure that the values of fields conform to the expected
format.

This will be subject to consolidation and refactoring over the next few months.
"""

import datetime
import json
import logging
import re
import sys
import urllib.parse
import warnings
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple, Union

import jsonschema
import pendulum
import yaml

from prefect._internal.pydantic import HAS_PYDANTIC_V2
from prefect._internal.schemas.fields import DateTimeTZ
from prefect.events.schemas.events import RelatedResource
from prefect.exceptions import InvalidNameError, InvalidRepositoryURLError
from prefect.utilities.annotations import NotSet
from prefect.utilities.filesystem import relative_path_to_current_platform
from prefect.utilities.importtools import from_qualified_name
from prefect.utilities.names import generate_slug
from prefect.utilities.pydantic import JsonPatch

BANNED_CHARACTERS = ["/", "%", "&", ">", "<"]
LOWERCASE_LETTERS_NUMBERS_AND_DASHES_ONLY_REGEX = "^[a-z0-9-]*$"
LOWERCASE_LETTERS_NUMBERS_AND_UNDERSCORES_REGEX = "^[a-z0-9_]*$"

if TYPE_CHECKING:
    from prefect.blocks.core import Block
    from prefect.events.schemas import DeploymentTrigger
    from prefect.utilities.callables import ParameterSchema

    if HAS_PYDANTIC_V2:
        from pydantic.v1.fields import ModelField
    else:
        from pydantic.fields import ModelField


def raise_on_name_with_banned_characters(name: str) -> str:
    """
    Raise an InvalidNameError if the given name contains any invalid
    characters.
    """
    if name is not None:
        if any(c in name for c in BANNED_CHARACTERS):
            raise InvalidNameError(
                f"Name {name!r} contains an invalid character. "
                f"Must not contain any of: {BANNED_CHARACTERS}."
            )
    return name


def raise_on_name_alphanumeric_dashes_only(
    value: str, field_name: str = "value"
) -> str:
    if not bool(re.match(LOWERCASE_LETTERS_NUMBERS_AND_DASHES_ONLY_REGEX, value)):
        raise ValueError(
            f"{field_name} must only contain lowercase letters, numbers, and dashes."
        )
    return value


def raise_on_name_alphanumeric_underscores_only(value, field_name: str = "value"):
    if not bool(re.match(LOWERCASE_LETTERS_NUMBERS_AND_UNDERSCORES_REGEX, value)):
        raise ValueError(
            f"{field_name} must only contain lowercase letters, numbers, and"
            " underscores."
        )
    return value


def validate_schema(schema: dict):
    """
    Validate that the provided schema is a valid json schema.

    Args:
        schema: The schema to validate.

    Raises:
        ValueError: If the provided schema is not a valid json schema.

    """
    try:
        if schema is not None:
            # Most closely matches the schemas generated by pydantic
            jsonschema.Draft4Validator.check_schema(schema)
    except jsonschema.SchemaError as exc:
        raise ValueError(
            "The provided schema is not a valid json schema. Schema error:"
            f" {exc.message}"
        ) from exc


def validate_values_conform_to_schema(
    values: dict, schema: dict, ignore_required: bool = False
):
    """
    Validate that the provided values conform to the provided json schema.

    Args:
        values: The values to validate.
        schema: The schema to validate against.
        ignore_required: Whether to ignore the required fields in the schema. Should be
            used when a partial set of values is acceptable.

    Raises:
        ValueError: If the parameters do not conform to the schema.

    """
    from prefect.utilities.collections import remove_nested_keys

    if ignore_required:
        schema = remove_nested_keys(["required"], schema)

    try:
        if schema is not None and values is not None:
            jsonschema.validate(values, schema)
    except jsonschema.ValidationError as exc:
        if exc.json_path == "$":
            error_message = "Validation failed."
        else:
            error_message = (
                f"Validation failed for field {exc.json_path.replace('$.', '')!r}."
            )
        error_message += f" Failure reason: {exc.message}"
        raise ValueError(error_message) from exc
    except jsonschema.SchemaError as exc:
        raise ValueError(
            "The provided schema is not a valid json schema. Schema error:"
            f" {exc.message}"
        ) from exc


### DEPLOYMENT SCHEMA VALIDATORS ###


def infrastructure_must_have_capabilities(
    value: Union[Dict[str, Any], "Block", None],
) -> Optional["Block"]:
    """
    Ensure that the provided value is an infrastructure block with the required capabilities.
    """

    from prefect.blocks.core import Block

    if isinstance(value, dict):
        if "_block_type_slug" in value:
            # Replace private attribute with public for dispatch
            value["block_type_slug"] = value.pop("_block_type_slug")
        block = Block(**value)
    elif value is None:
        return value
    else:
        block = value

    if "run-infrastructure" not in block.get_block_capabilities():
        raise ValueError(
            "Infrastructure block must have 'run-infrastructure' capabilities."
        )
    return block


def storage_must_have_capabilities(
    value: Union[Dict[str, Any], "Block", None],
) -> Optional["Block"]:
    """
    Ensure that the provided value is a storage block with the required capabilities.
    """
    from prefect.blocks.core import Block

    if isinstance(value, dict):
        block_type = Block.get_block_class_from_key(value.pop("_block_type_slug"))
        block = block_type(**value)
    elif value is None:
        return value
    else:
        block = value

    capabilities = block.get_block_capabilities()
    if "get-directory" not in capabilities:
        raise ValueError("Remote Storage block must have 'get-directory' capabilities.")
    return block


def handle_openapi_schema(value: Optional["ParameterSchema"]) -> "ParameterSchema":
    """
    This method ensures setting a value of `None` is handled gracefully.
    """
    from prefect.utilities.callables import ParameterSchema

    if value is None:
        return ParameterSchema()
    return value


def validate_parameters_conform_to_schema(value: dict, values: dict) -> dict:
    """Validate that the parameters conform to the parameter schema."""
    if values.get("enforce_parameter_schema"):
        validate_values_conform_to_schema(
            value, values.get("parameter_openapi_schema"), ignore_required=True
        )
    return value


def validate_parameter_openapi_schema(value: dict, values: dict) -> dict:
    """Validate that the parameter_openapi_schema is a valid json schema."""
    if values.get("enforce_parameter_schema"):
        validate_schema(value)
    return value


def return_none_schedule(v: Optional[Union[str, dict]]) -> Optional[Union[str, dict]]:
    from prefect.client.schemas.schedules import NoSchedule

    if isinstance(v, NoSchedule):
        return None
    return v


### SCHEDULE SCHEMA VALIDATORS ###


def validate_deprecated_schedule_fields(values: dict, logger: logging.Logger) -> dict:
    """
    Validate and log deprecation warnings for deprecated schedule fields.
    """
    if values.get("schedule") and not values.get("schedules"):
        logger.warning(
            "The field 'schedule' in 'Deployment' has been deprecated. It will not be "
            "available after Sep 2024. Define schedules in the `schedules` list instead."
        )
    elif values.get("is_schedule_active") and not values.get("schedules"):
        logger.warning(
            "The field 'is_schedule_active' in 'Deployment' has been deprecated. It will "
            "not be available after Sep 2024. Use the `active` flag within a schedule in "
            "the `schedules` list instead and the `pause` flag in 'Deployment' to pause "
            "all schedules."
        )
    return values


def reconcile_schedules(cls, values: dict) -> dict:
    """
    Reconcile the `schedule` and `schedules` fields in a deployment.
    """

    from prefect.deployments.schedules import (
        create_minimal_deployment_schedule,
        normalize_to_minimal_deployment_schedules,
    )

    schedule = values.get("schedule", NotSet)
    schedules = values.get("schedules", NotSet)

    if schedules is not NotSet:
        values["schedules"] = normalize_to_minimal_deployment_schedules(schedules)
    elif schedule is not NotSet:
        values["schedule"] = None

        if schedule is None:
            values["schedules"] = []
        else:
            values["schedules"] = [
                create_minimal_deployment_schedule(
                    schedule=schedule, active=values.get("is_schedule_active")
                )
            ]

    for schedule in values.get("schedules", []):
        cls._validate_schedule(schedule.schedule)

    return values


def interval_schedule_must_be_positive(v: datetime.timedelta) -> datetime.timedelta:
    if v.total_seconds() <= 0:
        raise ValueError("The interval must be positive")
    return v


def default_anchor_date(v: DateTimeTZ) -> DateTimeTZ:
    if v is None:
        return pendulum.now("UTC")
    return pendulum.instance(v)


def get_valid_timezones(v: str) -> Tuple[str, ...]:
    # pendulum.tz.timezones is a callable in 3.0 and above
    # https://github.com/PrefectHQ/prefect/issues/11619
    if callable(pendulum.tz.timezones):
        return pendulum.tz.timezones()
    else:
        return pendulum.tz.timezones


def validate_rrule_timezone(v: str) -> str:
    """
    Validate that the provided timezone is a valid IANA timezone.

    Unfortunately this list is slightly different from the list of valid
    timezones in pendulum that we use for cron and interval timezone validation.
    """
    from prefect._internal.pytz import HAS_PYTZ

    if HAS_PYTZ:
        import pytz
    else:
        from prefect._internal import pytz

    if v and v not in pytz.all_timezones_set:
        raise ValueError(f'Invalid timezone: "{v}"')
    elif v is None:
        return "UTC"
    return v


def validate_timezone(v: str, timezones: Tuple[str, ...]) -> str:
    if v and v not in timezones:
        raise ValueError(
            f'Invalid timezone: "{v}" (specify in IANA tzdata format, for example,'
            " America/New_York)"
        )
    return v


def default_timezone(v: str, values: Optional[dict] = {}) -> str:
    timezones = get_valid_timezones(v)

    if v is not None:
        return validate_timezone(v, timezones)

    # anchor schedules
    elif v is None and values and values.get("anchor_date"):
        tz = values["anchor_date"].tz.name
        if tz in timezones:
            return tz
        # sometimes anchor dates have "timezones" that are UTC offsets
        # like "-04:00". This happens when parsing ISO8601 strings.
        # In this case we, the correct inferred localization is "UTC".
        else:
            return "UTC"

    # cron schedules
    return v


def validate_cron_string(v: str) -> str:
    from croniter import croniter

    # croniter allows "random" and "hashed" expressions
    # which we do not support https://github.com/kiorky/croniter
    if not croniter.is_valid(v):
        raise ValueError(f'Invalid cron string: "{v}"')
    elif any(c for c in v.split() if c.casefold() in ["R", "H", "r", "h"]):
        raise ValueError(
            f'Random and Hashed expressions are unsupported, received: "{v}"'
        )
    return v


# approx. 1 years worth of RDATEs + buffer
MAX_RRULE_LENGTH = 6500


def validate_rrule_string(v: str) -> str:
    import dateutil.rrule

    # attempt to parse the rrule string as an rrule object
    # this will error if the string is invalid
    try:
        dateutil.rrule.rrulestr(v, cache=True)
    except ValueError as exc:
        # rrules errors are a mix of cryptic and informative
        # so reraise to be clear that the string was invalid
        raise ValueError(f'Invalid RRule string "{v}": {exc}')
    if len(v) > MAX_RRULE_LENGTH:
        raise ValueError(
            f'Invalid RRule string "{v[:40]}..."\n'
            f"Max length is {MAX_RRULE_LENGTH}, got {len(v)}"
        )
    return v


### AUTOMATION SCHEMA VALIDATORS ###


def validate_trigger_within(
    value: datetime.timedelta, field: "ModelField"
) -> datetime.timedelta:
    """
    Validate that the `within` field is greater than the minimum value.
    """
    minimum = field.field_info.extra["minimum"]
    if value.total_seconds() < minimum:
        raise ValueError("The minimum `within` is 0 seconds")
    return value


def validate_automation_names(
    field_value: List["DeploymentTrigger"], values: dict
) -> List["DeploymentTrigger"]:
    """
    Ensure that each trigger has a name for its automation if none is provided.
    """
    for i, trigger in enumerate(field_value, start=1):
        if trigger.name is None:
            trigger.name = f"{values['name']}__automation_{i}"

    return field_value


### INFRASTRUCTURE SCHEMA VALIDATORS ###


def validate_k8s_job_required_components(cls, value: Dict[str, Any]):
    """
    Validate that a Kubernetes job manifest has all required components.
    """
    from prefect.utilities.pydantic import JsonPatch

    patch = JsonPatch.from_diff(value, cls.base_job_manifest())
    missing_paths = sorted([op["path"] for op in patch if op["op"] == "add"])
    if missing_paths:
        raise ValueError(
            "Job is missing required attributes at the following paths: "
            f"{', '.join(missing_paths)}"
        )
    return value


def validate_k8s_job_compatible_values(cls, value: Dict[str, Any]):
    """
    Validate that the provided job values are compatible with the job type.
    """
    from prefect.utilities.pydantic import JsonPatch

    patch = JsonPatch.from_diff(value, cls.base_job_manifest())
    incompatible = sorted(
        [
            f"{op['path']} must have value {op['value']!r}"
            for op in patch
            if op["op"] == "replace"
        ]
    )
    if incompatible:
        raise ValueError(
            "Job has incompatible values for the following attributes: "
            f"{', '.join(incompatible)}"
        )
    return value


def cast_k8s_job_customizations(
    cls, value: Union[JsonPatch, str, List[Dict[str, Any]]]
):
    if isinstance(value, list):
        return JsonPatch(value)
    elif isinstance(value, str):
        try:
            return JsonPatch(json.loads(value))
        except json.JSONDecodeError as exc:
            raise ValueError(
                f"Unable to parse customizations as JSON: {value}. Please make sure"
                " that the provided value is a valid JSON string."
            ) from exc
    return value


def set_default_namespace(values: dict) -> dict:
    """
    Set the default namespace for a Kubernetes job if not provided.
    """
    job = values.get("job")

    namespace = values.get("namespace")
    job_namespace = job["metadata"].get("namespace") if job else None

    if not namespace and not job_namespace:
        values["namespace"] = "default"

    return values


def set_default_image(values: dict) -> dict:
    """
    Set the default image for a Kubernetes job if not provided.
    """
    from prefect.utilities.dockerutils import get_prefect_image_name

    job = values.get("job")
    image = values.get("image")
    job_image = (
        job["spec"]["template"]["spec"]["containers"][0].get("image") if job else None
    )

    if not image and not job_image:
        values["image"] = get_prefect_image_name()

    return values


def get_or_create_state_name(v: str, values: dict) -> str:
    """If a name is not provided, use the type"""

    # if `type` is not in `values` it means the `type` didn't pass its own
    # validation check and an error will be raised after this function is called
    if v is None and values.get("type"):
        v = " ".join([v.capitalize() for v in values.get("type").value.split("_")])
    return v


def get_or_create_run_name(name):
    return name or generate_slug(2)


### FILESYSTEM SCHEMA VALIDATORS ###


def stringify_path(value: Union[str, Path]) -> str:
    if isinstance(value, Path):
        return str(value)
    return value


def validate_basepath(value: str) -> str:
    scheme, netloc, _, _, _ = urllib.parse.urlsplit(value)

    if not scheme:
        raise ValueError(f"Base path must start with a scheme. Got {value!r}.")

    if not netloc:
        raise ValueError(
            f"Base path must include a location after the scheme. Got {value!r}."
        )

    if scheme == "file":
        raise ValueError(
            "Base path scheme cannot be 'file'. Use `LocalFileSystem` instead for"
            " local file access."
        )

    return value


def validate_github_access_token(v: str, values: dict) -> str:
    """Ensure that credentials are not provided with 'SSH' formatted GitHub URLs.

    Note: validates `access_token` specifically so that it only fires when
    private repositories are used.
    """
    if v is not None:
        if urllib.parse.urlparse(values["repository"]).scheme != "https":
            raise InvalidRepositoryURLError(
                "Crendentials can only be used with GitHub repositories "
                "using the 'HTTPS' format. You must either remove the "
                "credential if you wish to use the 'SSH' format and are not "
                "using a private repository, or you must change the repository "
                "URL to the 'HTTPS' format. "
            )

    return v


### SERIALIZER SCHEMA VALIDATORS ###


def validate_picklelib(value: str) -> str:
    """
    Check that the given pickle library is importable and has dumps/loads methods.
    """
    try:
        pickler = from_qualified_name(value)
    except (ImportError, AttributeError) as exc:
        raise ValueError(
            f"Failed to import requested pickle library: {value!r}."
        ) from exc

    if not callable(getattr(pickler, "dumps", None)):
        raise ValueError(f"Pickle library at {value!r} does not have a 'dumps' method.")

    if not callable(getattr(pickler, "loads", None)):
        raise ValueError(f"Pickle library at {value!r} does not have a 'loads' method.")

    return value


def validate_picklelib_version(values: dict) -> dict:
    """
    Infers a default value for `picklelib_version` if null or ensures it matches
    the version retrieved from the `pickelib`.
    """
    picklelib = values.get("picklelib")
    picklelib_version = values.get("picklelib_version")

    if not picklelib:
        raise ValueError("Unable to check version of unrecognized picklelib module")

    pickler = from_qualified_name(picklelib)
    pickler_version = getattr(pickler, "__version__", None)

    if not picklelib_version:
        values["picklelib_version"] = pickler_version
    elif picklelib_version != pickler_version:
        warnings.warn(
            (
                f"Mismatched {picklelib!r} versions. Found {pickler_version} in the"
                f" environment but {picklelib_version} was requested. This may"
                " cause the serializer to fail."
            ),
            RuntimeWarning,
            stacklevel=3,
        )

    return values


def validate_picklelib_and_modules(values: dict) -> dict:
    """
    Prevents modules from being specified if picklelib is not cloudpickle
    """
    if values.get("picklelib") != "cloudpickle" and values.get("pickle_modules"):
        raise ValueError(
            "`pickle_modules` cannot be used without 'cloudpickle'. Got"
            f" {values.get('picklelib')!r}."
        )
    return values


def validate_dump_kwargs(value: dict) -> dict:
    # `default` is set by `object_encoder`. A user provided callable would make this
    # class unserializable anyway.
    if "default" in value:
        raise ValueError("`default` cannot be provided. Use `object_encoder` instead.")
    return value


def validate_load_kwargs(value: dict) -> dict:
    # `object_hook` is set by `object_decoder`. A user provided callable would make
    # this class unserializable anyway.
    if "object_hook" in value:
        raise ValueError(
            "`object_hook` cannot be provided. Use `object_decoder` instead."
        )
    return value


def cast_type_names_to_serializers(value):
    from prefect.serializers import Serializer

    if isinstance(value, str):
        return Serializer(type=value)
    return value


def validate_compressionlib(value: str) -> str:
    """
    Check that the given pickle library is importable and has compress/decompress
    methods.
    """
    try:
        compressor = from_qualified_name(value)
    except (ImportError, AttributeError) as exc:
        raise ValueError(
            f"Failed to import requested compression library: {value!r}."
        ) from exc

    if not callable(getattr(compressor, "compress", None)):
        raise ValueError(
            f"Compression library at {value!r} does not have a 'compress' method."
        )

    if not callable(getattr(compressor, "decompress", None)):
        raise ValueError(
            f"Compression library at {value!r} does not have a 'decompress' method."
        )

    return value


### SETTINGS SCHEMA VALIDATORS ###


def validate_settings(value: dict) -> dict:
    from prefect.settings import SETTING_VARIABLES, Setting

    if value is None:
        return value

    # Cast string setting names to variables
    validated = {}
    for setting, val in value.items():
        if isinstance(setting, str) and setting in SETTING_VARIABLES:
            validated[SETTING_VARIABLES[setting]] = val
        elif isinstance(setting, Setting):
            validated[setting] = val
        else:
            raise ValueError(f"Unknown setting {setting!r}.")

    return validated


def validate_yaml(value: Union[str, dict]) -> dict:
    if isinstance(value, str):
        return yaml.safe_load(value)
    return value


# TODO: if we use this elsewhere we can change the error message to be more generic
def list_length_50_or_less(v: Optional[List[float]]) -> Optional[List[float]]:
    if isinstance(v, list) and (len(v) > 50):
        raise ValueError("Can not configure more than 50 retry delays per task.")
    return v


# TODO: if we use this elsewhere we can change the error message to be more generic
def validate_not_negative(v: Optional[float]) -> Optional[float]:
    if v is not None and v < 0:
        raise ValueError("`retry_jitter_factor` must be >= 0.")
    return v


def validate_message_template_variables(v: Optional[str]) -> Optional[str]:
    from prefect.client.schemas.objects import FLOW_RUN_NOTIFICATION_TEMPLATE_KWARGS

    if v is not None:
        try:
            v.format(**{k: "test" for k in FLOW_RUN_NOTIFICATION_TEMPLATE_KWARGS})
        except KeyError as exc:
            raise ValueError(f"Invalid template variable provided: '{exc.args[0]}'")
    return v


def validate_default_queue_id_not_none(v: Optional[str]) -> Optional[str]:
    if v is None:
        raise ValueError(
            "`default_queue_id` is a required field. If you are "
            "creating a new WorkPool and don't have a queue "
            "ID yet, use the `actions.WorkPoolCreate` model instead."
        )
    return v


def validate_max_metadata_length(
    v: Optional[Dict[str, Any]],
) -> Optional[Dict[str, Any]]:
    max_metadata_length = 500
    if not isinstance(v, dict):
        return v
    for key in v.keys():
        if len(str(v[key])) > max_metadata_length:
            v[key] = str(v[key])[:max_metadata_length] + "..."
    return v


### DOCKER SCHEMA VALIDATORS ###


def validate_registry_url(value: Optional[str]) -> Optional[str]:
    if isinstance(value, str):
        if "://" not in value:
            return "https://" + value
    return value


def convert_labels_to_docker_format(labels: Dict[str, str]) -> Dict[str, str]:
    labels = labels or {}
    new_labels = {}
    for name, value in labels.items():
        if "/" in name:
            namespace, key = name.split("/", maxsplit=1)
            new_namespace = ".".join(reversed(namespace.split(".")))
            new_labels[f"{new_namespace}.{key}"] = value
        else:
            new_labels[name] = value
    return new_labels


def check_volume_format(volumes: List[str]) -> List[str]:
    for volume in volumes:
        if ":" not in volume:
            raise ValueError(
                "Invalid volume specification. "
                f"Expected format 'path:container_path', but got {volume!r}"
            )

    return volumes


### EVENTS SCHEMA VALIDATORS ###


def enforce_maximum_related_resources(
    value: List[RelatedResource],
) -> List[RelatedResource]:
    from prefect.settings import (
        PREFECT_EVENTS_MAXIMUM_RELATED_RESOURCES,
    )

    if len(value) > PREFECT_EVENTS_MAXIMUM_RELATED_RESOURCES.value():
        raise ValueError(
            "The maximum number of related resources "
            f"is {PREFECT_EVENTS_MAXIMUM_RELATED_RESOURCES.value()}"
        )

    return value


### TASK RUN SCHEMA VALIDATORS ###


def validate_cache_key_length(cache_key: Optional[str]) -> Optional[str]:
    from prefect.settings import (
        PREFECT_API_TASK_CACHE_KEY_MAX_LENGTH,
    )

    if cache_key and len(cache_key) > PREFECT_API_TASK_CACHE_KEY_MAX_LENGTH.value():
        raise ValueError(
            "Cache key exceeded maximum allowed length of"
            f" {PREFECT_API_TASK_CACHE_KEY_MAX_LENGTH.value()} characters."
        )
    return cache_key


### PYTHON ENVIRONMENT SCHEMA VALIDATORS ###


def infer_python_version(value: Optional[str]) -> Optional[str]:
    if value is None:
        return f"{sys.version_info.major}.{sys.version_info.minor}"
    return value


def return_v_or_none(v: Optional[str]) -> Optional[str]:
    """Make sure that empty strings are treated as None"""
    if not v:
        return None
    return v


### INFRASTRUCTURE BLOCK SCHEMA VALIDATORS ###


def validate_block_is_infrastructure(v: "Block") -> "Block":
    from prefect.infrastructure.base import Infrastructure

    print("v: ", v)
    if not isinstance(v, Infrastructure):
        raise TypeError("Provided block is not a valid infrastructure block.")

    return v


### PROCESS JOB CONFIGURATION VALIDATORS ###


def validate_command(v: str) -> Path:
    """Make sure that the working directory is formatted for the current platform."""
    if v:
        return relative_path_to_current_platform(v)
    return v
