# generic aggregator for wizard forms and helpers

from .ux import apply_ux_hints

from .project_settings import ProjectSettingsForm
from .project_settings import DefaultProjectSettingsForm
from .project_settings import UnifiedProjectSettingsForm
from .project_settings import SingleVpcProjectSettingsForm
from .project_settings import RgsProjectSettingsForm

from .repository_settings import RepositorySettingsForm
from .spacelift_settings import SpaceliftSettingsForm

from .replacements import ReplacementsForm
from .jinja2_config import Jinja2Form
from .common_settings import CommonSettingsForm
from .grafana_settings import GrafanaSettingsForm
from .rancher_settings import RancherSettingsForm
from .vpc_settings import VpcSettingsForm
from .availability_zones import AvailabilityZonesForm
from .filter_az_zone_ids import FilterAzZoneIDsForm
from .subnets import SubnetsForm
from .subnets_default import DefaultSubnetsForm
from .eks_settings import EksSettingsForm
from .aurora_settings import AuroraSettingsForm
from .redis_form import RedisForm
from .alb_form import AlbForm
from .formkiq_form import FormkiqForm
from .mkodo_form import MkodoForm
from .kafka_form import KafkaForm
from .vault_database import VaultDatabaseForm
from .sumologic_form import SumologicForm

__all__ = [
    "apply_ux_hints",
    "UnifiedProjectSettingsForm", "SingleVpcProjectSettingsForm",
    "RgsProjectSettingsForm", "ProjectSettingsForm", "DefaultProjectSettingsForm",
    "RepositorySettingsForm", "SpaceliftSettingsForm",
    "ReplacementsForm", "Jinja2Form", "CommonSettingsForm",
    "GrafanaSettingsForm", "RancherSettingsForm", "VpcSettingsForm",
    "AvailabilityZonesForm", "FilterAzZoneIDsForm", "SubnetsForm",
    "DefaultSubnetsForm", "EksSettingsForm", "AuroraSettingsForm", "RedisForm",
    "AlbForm", "FormkiqForm", "MkodoForm", "KafkaForm", "VaultDatabaseForm",
    "SumologicForm",
]
