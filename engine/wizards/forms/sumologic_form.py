# forms/sumologic_form.py
import json
from flask_wtf import FlaskForm
from wtforms import StringField, TextAreaField, SelectField, FormField
from wtforms.validators import DataRequired
from .validators import is_json
from engine.wizards.constants.project_constants import ENV_DEFAULT_ENV_TYPES
from engine.wizards.forms.ux import get_env_key, get_env_label

DEFAULT_FIREHOSES = [
    {"FILTER_PATTERN":"", "LOG_GROUP":"/aws/rds/cluster/<<PREFIX>>/postgresql",
     "NAME":"db","S3_BACKUP_PREFIX":"database/!{timestamp:yyyy}/!{timestamp:MM}/!{timestamp:dd}/!{timestamp:HH}/",
     "S3_FAILED_PREFIX":"database/http-endpoint-failed/","SUMOLOGIC_ENDPOINT":"firehose_db"},
    {"FILTER_PATTERN":'{ $.kind = "Event" }',"LOG_GROUP":"/aws/eks/<<PREFIX>>/cluster",
     "NAME":"eks-audit","S3_BACKUP_PREFIX":"eks/audit/!{timestamp:yyyy}/!{timestamp:MM}/!{timestamp:dd}/!{timestamp:HH}/",
     "S3_FAILED_PREFIX":"eks/audit/http-endpoint-failed/","SUMOLOGIC_ENDPOINT":"firehose_eks_audit"},
    {"FILTER_PATTERN":"%^[a-zA-Z0-9].*%","LOG_GROUP":"/aws/eks/<<PREFIX>>/cluster",
     "NAME":"eks-auth-apiserver","S3_BACKUP_PREFIX":"eks/auth/!{timestamp:yyyy}/!{timestamp:MM}/!{timestamp:dd}/!{timestamp:HH}/",
     "S3_FAILED_PREFIX":"eks/auth/http-endpoint-failed/","SUMOLOGIC_ENDPOINT":"firehose_eks_auth"},
    {"FILTER_PATTERN":"","LOG_GROUP":"/aws/rds/instance/vault-<<PREFIX>>/postgresql",
     "NAME":"vault-db","S3_BACKUP_PREFIX":"vault/database/!{timestamp:yyyy}/!{timestamp:MM}/!{timestamp:dd}/!{timestamp:HH}/",
     "S3_FAILED_PREFIX":"vault/database/http-endpoint-failed/","SUMOLOGIC_ENDPOINT":"firehose_hashcorp_vault_db"},
    {"FILTER_PATTERN":"","LOG_GROUP":"/aws/application/checksum/reports",
     "NAME":"container-integrity","S3_BACKUP_PREFIX":"container_integrity/!{timestamp:yyyy}/!{timestamp:MM}/!{timestamp:dd}/!{timestamp:HH}/",
     "S3_FAILED_PREFIX":"container_integrity/http-endpoint-failed/","SUMOLOGIC_ENDPOINT":"firehose_container_integrity"},
    {"FILTER_PATTERN":"","LOG_GROUP":"/aws/aide/fim/reports",
     "NAME":"fim","S3_BACKUP_PREFIX":"fim/!{timestamp:yyyy}/!{timestamp:MM}/!{timestamp:dd}/!{timestamp:HH}/",
     "S3_FAILED_PREFIX":"fim/http-endpoint-failed/","SUMOLOGIC_ENDPOINT":"firehose_fim"},
    {"FILTER_PATTERN":"","LOG_GROUP":"aws-waf-logs-<<PREFIX>>",
     "NAME":"waf","S3_BACKUP_PREFIX":"waf/!{timestamp:yyyy}/!{timestamp:MM}/!{timestamp:dd}/!{timestamp:HH}/",
     "S3_FAILED_PREFIX":"waf/http-endpoint-failed/","SUMOLOGIC_ENDPOINT":"firehose_waf"},
]

class SumologicEnvForm(FlaskForm):
    class Meta:
        csrf = False  
    """Per-environment Sumo settings (includes former Checksum Report bucket)."""
    # split all fields by env
    sumologic_aws_account_id = StringField(
        "AWS Account ID", validators=[DataRequired()], default="926226587429"
    )
    secret_manager_name = StringField(
        "Secrets Manager Name", validators=[DataRequired()],
        default="/shared/sumologic-endpoints"
    )

    endpoints_tokens = TextAreaField(
        "Endpoints Tokens JSON", validators=[is_json()],
        default='{"firehose_fim":"","firehose_db":"","firehose_hashcorp_vault_db":"",'
                '"firehose_eks_auth":"","firehose_eks_audit":"","firehose_container_integrity":"","firehose_waf":""}',
        render_kw={"rows": 6,
                   "placeholder": '{ "firehose_fim": "https://...", "firehose_db": "https://..." }'}
    )
    create_endpoint_tokens = SelectField(
        "Create Endpoint Tokens?", choices=[("true","true"),("false","false")], default="true"
    )

    firehoses = TextAreaField(
        "Firehoses Config JSON", validators=[DataRequired(), is_json()],
        default=json.dumps(DEFAULT_FIREHOSES, indent=2),
        render_kw={"rows": 10,
                   "placeholder": '[{ "LOG_GROUP": "...", "NAME": "db", "SUMOLOGIC_ENDPOINT": "..." } ]'}
    )

    api_gateway_user_name = StringField(
        "API Gateway User", default="sumologic-apigateway"
    )
    api_gateway = TextAreaField(
        "API Gateway JSON", validators=[is_json()],
        default=('{"NAME":"sumologic-apigateway","LAMBDAS":[{"NAME":"sumologic-email-lambda",'
                 '"IMAGE_URL":"580260895509.dkr.ecr.us-east-1.amazonaws.com/aws-sumo-email-lambda:1205.15.10",'
                 '"METHOD":"POST","ROUTE":"/emails","TIMEOUT":30,"ENV_VARIABLES":{"AWS_SUMO_EMAIL_SENDER":"soc@praxis.ca"}}]}'),
        render_kw={"rows": 6}
    )

    ses_emails_identity = TextAreaField(
        "SES Identities", validators=[is_json()],
        default='["soc@praxis.ca"]', render_kw={"rows": 3}
    )

    create_fim_log_group = SelectField(
        "Create FIM Log Group?", choices=[("true","true"),("false","false")], default="true"
    )
    create_app_integrity_log_group = SelectField(
        "Create App Integrity Log Group?", choices=[("true","true"),("false","false")], default="true"
    )

    # <<< MOVED from ChecksumReportForm
    sumologic_s3_bucket = StringField(
        "Checksum Report S3 Bucket",
        validators=[DataRequired()],
        render_kw={"placeholder": "<<PREFIX>>-sumologic-logs-<env>"},
        description="Auto-filled to <<PREFIX>>-sumologic-logs-<env>."
    )

class SumologicForm(FlaskForm):
    """Container that becomes dynamic: one subform per selected environment."""
    @classmethod
    def for_envs(cls, env_types):
        env_types = env_types or ENV_DEFAULT_ENV_TYPES
        attrs = {get_env_key(env): FormField(SumologicEnvForm, description=f"Sumologic settings for {get_env_label(env)}") 
                 for env in env_types}
        return type("SumologicFormDynamic", (cls,), attrs)
