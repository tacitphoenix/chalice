import json
import os

import botocore.session
import mock
import pytest
from botocore.stub import Stubber
from pytest import fixture

from chalice.app import Chalice
from chalice.awsclient import TypedAWSClient
from chalice.config import Config
from chalice.deploy.deployer import APIGatewayDeployer
from chalice.deploy.deployer import ApplicationPolicyHandler
from chalice.deploy.deployer import Deployer
from chalice.deploy.deployer import LambdaDeployer
from chalice.deploy.deployer import NoPrompt
from chalice.deploy.deployer import validate_configuration
from chalice.deploy.deployer import validate_routes
from chalice.deploy.packager import LambdaDeploymentPackager
from chalice.deploy.swagger import SwaggerGenerator

_SESSION = None


class SimpleStub(object):
    def __init__(self, stubber):
        pass


class InMemoryOSUtils(object):
    def __init__(self, filemap=None):
        if filemap is None:
            filemap = {}
        self.filemap = filemap

    def file_exists(self, filename):
        return filename in self.filemap

    def get_file_contents(self, filename, binary=True):
        return self.filemap[filename]

    def set_file_contents(self, filename, contents, binary=True):
        self.filemap[filename] = contents


@fixture
def stubbed_api_gateway():
    return stubbed_client('apigateway')


@fixture
def stubbed_lambda():
    return stubbed_client('lambda')


@fixture
def sample_app():
    app = Chalice('sample')

    @app.route('/')
    def foo():
        return {}

    return app


@fixture
def in_memory_osutils():
    return InMemoryOSUtils()


@fixture
def app_policy(in_memory_osutils):
    return ApplicationPolicyHandler(in_memory_osutils)


@fixture
def swagger_gen():
    return SwaggerGenerator(region='us-west-2',
                            lambda_arn='lambda_arn')


def stubbed_client(service_name):
    global _SESSION
    if _SESSION is None:
        _SESSION = botocore.session.get_session()
    client = _SESSION.create_client(service_name,
                                    region_name='us-west-2')
    stubber = Stubber(client)
    return client, stubber


def test_trailing_slash_routes_result_in_error():
    app = Chalice('appname')
    app.routes = {'/trailing-slash/': None}
    config = Config.create(chalice_app=app)
    with pytest.raises(ValueError):
        validate_configuration(config)


def test_manage_iam_role_false_requires_role_arn(sample_app):
    config = Config.create(chalice_app=sample_app, manage_iam_role=False,
                           iam_role_arn='arn:::foo')
    assert validate_configuration(config) is None


def test_validation_error_if_no_role_provided_when_manage_false(sample_app):
    # We're indicating that we should not be managing the
    # IAM role, but we're not giving a role ARN to use.
    # This is a validation error.
    config = Config.create(chalice_app=sample_app, manage_iam_role=False)
    with pytest.raises(ValueError):
        validate_configuration(config)


def test_can_deploy_apig_and_lambda(sample_app):
    lambda_deploy = mock.Mock(spec=LambdaDeployer)
    apig_deploy = mock.Mock(spec=APIGatewayDeployer)

    apig_deploy.deploy.return_value = ('api_id', 'region', 'stage')

    d = Deployer(apig_deploy, lambda_deploy)
    cfg = Config({'chalice_app': sample_app})
    result = d.deploy(cfg)
    assert result == ('api_id', 'region', 'stage')
    lambda_deploy.deploy.assert_called_with(cfg)
    apig_deploy.deploy.assert_called_with(cfg)


def test_noprompt_always_returns_default():
    assert not NoPrompt().confirm("You sure you want to do this?",
                                  default=False)
    assert NoPrompt().confirm("You sure you want to do this?",
                              default=True)
    assert NoPrompt().confirm("You sure?", default='yes') == 'yes'


def test_lambda_deployer_repeated_deploy(app_policy):
    osutils = InMemoryOSUtils({'packages.zip': b'package contents'})
    aws_client = mock.Mock(spec=TypedAWSClient)
    packager = mock.Mock(spec=LambdaDeploymentPackager)

    packager.deployment_package_filename.return_value = 'packages.zip'
    # Given the lambda function already exists:
    aws_client.lambda_function_exists.return_value = True
    # And given we don't want chalice to manage our iam role for the lambda
    # function:
    cfg = Config({'chalice_app': sample_app, 'manage_iam_role': False,
                  'app_name': 'appname', 'iam_role_arn': True,
                  'project_dir': './myproject'})

    d = LambdaDeployer(aws_client, packager, None, osutils, app_policy)
    # Doing a lambda deploy:
    d.deploy(cfg)

    # Should result in injecting the latest app code.
    packager.inject_latest_app.assert_called_with('packages.zip',
                                                  './myproject')

    # And should result in the lambda function being updated with the API.
    aws_client.update_function_code.assert_called_with(
        'appname', 'package contents')


def test_cant_have_options_with_cors(sample_app):
    @sample_app.route('/badcors', methods=['GET', 'OPTIONS'], cors=True)
    def badview():
        pass

    with pytest.raises(ValueError):
        validate_routes(sample_app.routes)


def test_policy_autogenerated_when_enabled(app_policy,
                                           in_memory_osutils):
    cfg = Config.create(autogen_policy=True, project_dir='.')
    in_memory_osutils.filemap['./app.py'] = ''
    generated = app_policy.generate_policy_from_app_source(cfg)
    # We don't actually need to validate the exact policy, we'll just
    # check that it looks ok.
    assert 'Statement' in generated
    assert 'Version' in generated


def test_no_policy_generated_when_disabled_in_config(app_policy,
                                                     in_memory_osutils):
    previous_policy = '{"Statement": ["foo"]}'
    filename = os.path.join('.', '.chalice', 'policy.json')
    in_memory_osutils.filemap[filename] = previous_policy
    cfg = Config.create(autogen_policy=False, project_dir='.')
    generated = app_policy.generate_policy_from_app_source(cfg)
    assert generated == json.loads(previous_policy)


def test_load_last_policy_when_file_does_not_exist(app_policy):
    cfg = Config.create(project_dir='.')
    loaded = app_policy.load_last_policy(cfg)
    assert loaded == {
        "Statement": [],
        "Version": "2012-10-17",
    }


def test_load_policy_from_disk_when_file_exists(app_policy,
                                                in_memory_osutils):
    previous_policy = '{"Statement": ["foo"]}'
    filename = os.path.join('.', '.chalice', 'policy.json')
    in_memory_osutils.filemap[filename] = previous_policy
    cfg = Config.create(project_dir='.')
    loaded = app_policy.load_last_policy(cfg)
    assert loaded == json.loads(previous_policy)


def test_can_record_policy_to_disk(app_policy):
    cfg = Config.create(project_dir='.')
    latest_policy = {"Statement": ["policy"]}
    app_policy.record_policy(cfg, latest_policy)
    assert app_policy.load_last_policy(cfg) == latest_policy


def test_can_produce_swagger_top_level_keys(sample_app, swagger_gen):
    swagger_doc = swagger_gen.generate_swagger(sample_app)
    assert swagger_doc['swagger'] == '2.0'
    assert swagger_doc['info']['title'] == 'sample'
    assert swagger_doc['schemes'] == ['https']
    assert '/' in swagger_doc['paths'], swagger_doc['paths']
    index_config = swagger_doc['paths']['/']
    assert 'get' in index_config


def test_can_produce_doc_for_method(sample_app, swagger_gen):
    doc = swagger_gen.generate_swagger(sample_app)
    single_method = doc['paths']['/']['get']
    assert single_method['consumes'] == ['application/json']
    assert single_method['produces'] == ['application/json']
    # 'responses' is validated in a separate test,
    # it's all boilerplate anyways.
    # Same for x-amazon-apigateway-integration.


def test_apigateway_integration_generation(sample_app, swagger_gen):
    doc = swagger_gen.generate_swagger(sample_app)
    single_method = doc['paths']['/']['get']
    apig_integ = single_method['x-amazon-apigateway-integration']
    assert apig_integ['passthroughBehavior'] == 'when_no_match'
    assert apig_integ['httpMethod'] == 'POST'
    assert apig_integ['type'] == 'aws_proxy'
    assert apig_integ['uri'] == (
        "arn:aws:apigateway:us-west-2:lambda:path"
        "/2015-03-31/functions/lambda_arn/invocations"
    )
    assert 'responses' in apig_integ
    responses = apig_integ['responses']
    assert responses['default'] == {'statusCode': '200'}


def test_can_add_url_captures_to_params(sample_app, swagger_gen):
    @sample_app.route('/path/{capture}')
    def foo(name):
        return {}

    doc = swagger_gen.generate_swagger(sample_app)
    single_method = doc['paths']['/path/{capture}']['get']
    apig_integ = single_method['x-amazon-apigateway-integration']
    assert 'parameters' in apig_integ
    assert apig_integ['parameters'] == [
        {'name': "capture", "in": "path", "required": True, "type": "string"}
    ]


def test_can_add_multiple_http_methods(sample_app, swagger_gen):
    @sample_app.route('/multimethod', methods=['GET', 'POST'])
    def multiple_methods():
        pass

    doc = swagger_gen.generate_swagger(sample_app)
    view_config = doc['paths']['/multimethod']
    assert 'get' in view_config
    assert 'post' in view_config
    assert view_config['get'] == view_config['post']


def test_can_add_preflight_cors(sample_app, swagger_gen):
    @sample_app.route('/cors', methods=['GET', 'POST'], cors=True)
    def cors_request():
        pass

    doc = swagger_gen.generate_swagger(sample_app)
    view_config = doc['paths']['/cors']
    # We should add an OPTIONS preflight request automatically.
    assert 'options' in view_config, (
        'Preflight OPTIONS method not added to CORS view')
    options = view_config['options']
    expected_response_params = {
        'method.response.header.Access-Control-Allow-Methods': (
            "'GET,POST,OPTIONS'"),
        'method.response.header.Access-Control-Allow-Headers': (
            "'Content-Type,X-Amz-Date,Authorization,"
            "X-Api-Key,X-Amz-Security-Token'"),
        'method.response.header.Access-Control-Allow-Origin': "'*'",
    }
    assert options == {
        'consumes': ['application/json'],
        'produces': ['application/json'],
        'responses': {
            '200': {
                'description': '200 response',
                'schema': {
                    '$ref': '#/definitions/Empty'
                },
                'headers': {
                    'Access-Control-Allow-Origin': {'type': 'string'},
                    'Access-Control-Allow-Methods': {'type': 'string'},
                    'Access-Control-Allow-Headers': {'type': 'string'},
                }
            }
        },
        'x-amazon-apigateway-integration': {
            'responses': {
                'default': {
                    'statusCode': '200',
                    'responseParameters': expected_response_params,
                }
            },
            'requestTemplates': {
                'application/json': '{"statusCode": 200}'
            },
            'passthroughBehavior': 'when_no_match',
            'type': 'mock',
        },
    }
