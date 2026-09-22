# forms/main.py

from flask_wtf import FlaskForm
from wtforms import FormField
from .project_settings import ProjectSettingsForm, DefaultProjectSettingsForm, RepositorySettingsForm
from .spacelift_settings import SpaceliftSettingsForm
from engine.wizards.constants.project_constants import ENV_DEFAULT_ENV_TYPES
from .common_settings import CommonSettingsForm
from .rancher_settings import RancherSettingsForm
from .vpc_settings import VpcEnvForm, VpcSettingsForm
from .availability_zones import AvailabilityZonesForm
from .filter_az_zone_ids import FilterAzZoneIDsForm
from .subnets import SubnetsForm
from .eks_settings import EksSettingsForm
from .aurora_settings import AuroraSettingsForm
from .redis_form import RedisForm
from .external_alb_ips import ExternalAlbIpsForm
from .alb_form import AlbForm
from .replacements import ReplacementsForm
from .whitelists import WhitelistsForm
from .formkiq_form import FormkiqForm
from .mkodo_form import MkodoForm
from .vault_database import VaultDatabaseForm
from .sumologic_form import SumologicForm

class MainConfigForm(FlaskForm):
    project_settings             = FormField(ProjectSettingsForm)
    project_settings             = FormField(DefaultProjectSettingsForm)
    repository_settings          = FormField(RepositorySettingsForm)
    spacelift_settings           = FormField(SpaceliftSettingsForm)   # base placeholder
    replacements                 = FormField(ReplacementsForm)
    whitelists                   = FormField(WhitelistsForm)
    common                       = FormField(CommonSettingsForm)
    rancher                      = FormField(RancherSettingsForm)     # base placeholder
    vpc                          = FormField(VpcEnvForm)
    availability_zones           = FormField(AvailabilityZonesForm)
    filter_az_zone_ids           = FormField(FilterAzZoneIDsForm)
    subnets                      = FormField(SubnetsForm)             # base placeholder
    eks                          = FormField(EksSettingsForm)         # base placeholder
    aurora                       = FormField(AuroraSettingsForm)
    redis                        = FormField(RedisForm)
    external_alb_allowed_ips     = FormField(ExternalAlbIpsForm)
    alb                          = FormField(AlbForm)
    formkiq                      = FormField(FormkiqForm)
    mkodo                        = FormField(MkodoForm)
    vault_database               = FormField(VaultDatabaseForm)
    sumologic                    = FormField(SumologicForm)


    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        # Which envs are selected in Project Settings?
        envs = self.project_settings.environment_type.data or ENV_DEFAULT_ENV_TYPES

        # >>> NEW: helper to swap a FormField's .form with a dynamic subclass instance
        def _swap_dynamic(form_field, base_cls, *, init_kwargs=None):
            """
            If the base form exposes .for_envs, instantiate the dynamic subclass
            and assign it to the FormField; otherwise, fall back to the base form.
            """
            init_kwargs = init_kwargs or {}
            if hasattr(base_cls, "for_envs"):
                Dynamic = base_cls.for_envs(envs)
                form_field.form = Dynamic(**init_kwargs)
            else:
                # Backward-compat fallback for older forms that still accept env_types
                try:
                    form_field.form = base_cls(env_types=envs, **init_kwargs)
                except TypeError:
                    form_field.form = base_cls(**init_kwargs)

        def _common_defaults():
            common_form = getattr(self.common, "form", None)
            if not common_form:
                return {}

            def _data(name):
                field = getattr(common_form, name, None)
                if not field:
                    return ""
                return field.data or ""

            return {
                "domain_name": _data("domain_name"),
                "environment": _data("environment"),
                "product": _data("product"),
                "customer": _data("customer"),
                "support_organization": _data("support_organization"),
            }

        # >>> CHANGED: use dynamic forms for env-aware steps
        _swap_dynamic(self.spacelift_settings, SpaceliftSettingsForm)
        _swap_dynamic(self.subnets,            SubnetsForm)
        _swap_dynamic(self.eks,                EksSettingsForm)
        _swap_dynamic(
            self.rancher,
            RancherSettingsForm,
            init_kwargs={"common_defaults": _common_defaults()},
        )
        _swap_dynamic(self.vpc, VpcSettingsForm)

        # Non env-scoped forms remain unchanged.
