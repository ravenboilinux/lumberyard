#
# All or portions of this file Copyright (c) Amazon.com, Inc. or its affiliates or
# its licensors.
#
# For complete copyright and license terms please see the LICENSE at the root of this
# distribution (the "License"). All use of this software is governed by the License,
# or, if provided, by the license below or the license accompanying this file. Do not
# remove or modify any license notices. This file is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#
# $Revision: #2 $

# Suppress "Parent module 'x' not found while handling absolute import " warnings.
from __future__ import absolute_import

import json
# Python 2.7/3.7 Compatibility
import six

import boto3
from botocore.exceptions import ClientError

from cgf_utils import properties
from cgf_utils import custom_resource_response
from cgf_utils import aws_utils
from cgf_utils import custom_resource_utils
from resource_manager_common import constant
from resource_types.cognito import identity_pool
from resource_manager_common import stack_info
from botocore.client import Config


def handler(event, context):
    """Entry point for the Custom::CognitoIdentityPool resource handler."""
    stack_id = event['StackId']

    props = properties.load(event, {
        'ConfigurationBucket': properties.String(),
        'ConfigurationKey': properties.String(),  # this is only here to force the resource handler to execute on each update to the deployment
        'IdentityPoolName': properties.String(),
        'UseAuthSettingsObject': properties.String(),
        'AllowUnauthenticatedIdentities': properties.String(),
        'DeveloperProviderName': properties.String(default=''),
        'ShareMode': properties.String(default=''),  # SHARED when the pool from the file should be used
        'Roles': properties.Object(default={}, schema={'*': properties.String()}),
        'RoleMappings': properties.Object(default={},
                                          schema={
                                              'Cognito': properties.Object(default={}, schema={
                                                  'Type': properties.String(''),
                                                  'AmbiguousRoleResolution': properties.String('')
                                              })
                                          })
    })

    # give the identity pool a unique name per stack
    stack_manager = stack_info.StackInfoManager()
    stack = stack_manager.get_stack_info(stack_id)

    # Set up resource tags for all resources created
    tags = {
        constant.PROJECT_NAME_TAG: stack.project_stack.project_name,
        constant.STACK_ID_TAG: stack_id
    }

    shared_pool = aws_utils.get_cognito_pool_from_file(
        props.ConfigurationBucket,
        props.ConfigurationKey,
        event['LogicalResourceId'],
        stack
    )

    identity_pool_name = stack.stack_name + props.IdentityPoolName
    identity_pool_name = identity_pool_name.replace('-', ' ')
    identity_client = identity_pool.get_identity_client()
    identity_pool_id = custom_resource_utils.get_embedded_physical_id(event.get('PhysicalResourceId'))
    found_pool = identity_pool.get_identity_pool(identity_pool_id)

    request_type = event['RequestType']
    if shared_pool and props.ShareMode == 'SHARED':
        data = {
            'IdentityPoolName': identity_pool_name,
            'IdentityPoolId': shared_pool['PhysicalResourceId']
        }
        return custom_resource_response.success_response(data, shared_pool['PhysicalResourceId'])

    if request_type == 'Delete':
        if found_pool is not None:
            identity_client.delete_identity_pool(IdentityPoolId=identity_pool_id)
        data = {}

    else:
        use_auth_settings_object = props.UseAuthSettingsObject.lower() == 'true'
        supported_login_providers = {}

        if use_auth_settings_object:
            # download the auth settings from s3
            player_access_key = 'player-access/' + constant.AUTH_SETTINGS_FILENAME
            auth_doc = json.loads(_load_doc_from_s3(props.ConfigurationBucket, player_access_key))

            # if the doc has entries add them to the supported_login_providers dictionary
            if len(auth_doc) > 0:
                for key, value in six.iteritems(auth_doc):
                    supported_login_providers[value['provider_uri']] = value['app_id']

        cognito_identity_providers = identity_pool.get_cognito_identity_providers(stack_manager, stack_id, event['LogicalResourceId'])

        print('Identity Providers: {}'.format(cognito_identity_providers))
        allow_anonymous = props.AllowUnauthenticatedIdentities.lower() == 'true'
        # if the pool exists just update it, otherwise create a new one

        args = {
            'IdentityPoolName': identity_pool_name,
            'AllowUnauthenticatedIdentities': allow_anonymous,
            'SupportedLoginProviders': supported_login_providers,
            'CognitoIdentityProviders': cognito_identity_providers,
            'IdentityPoolTags': tags
        }

        if props.DeveloperProviderName:
            args['DeveloperProviderName'] = props.DeveloperProviderName

        if found_pool is not None:
            identity_client.update_identity_pool(IdentityPoolId=identity_pool_id, **args)
        else:
            response = identity_client.create_identity_pool(**args)
            identity_pool_id = response['IdentityPoolId']

        # update the roles for the pool
        role_mappings = {}
        if props.RoleMappings.Cognito.Type and len(cognito_identity_providers) > 0:
            print('Adding role mappings for Cognito {}'.format(props.RoleMappings.Cognito.__dict__))
            role_mappings['{}:{}'.format(cognito_identity_providers[0]['ProviderName'],
                                         cognito_identity_providers[0]['ClientId'])] = props.RoleMappings.Cognito.__dict__

        print("Role Mappings: {}".format(role_mappings))
        identity_client.set_identity_pool_roles(
            IdentityPoolId=identity_pool_id,
            Roles=props.Roles.__dict__,
            RoleMappings=role_mappings)

        data = {
            'IdentityPoolName': identity_pool_name,
            'IdentityPoolId': identity_pool_id
        }

    physical_resource_id = identity_pool_id

    return custom_resource_response.success_response(data, physical_resource_id)


def _get_s3_client():
    return boto3.client('s3', region_name=aws_utils.current_region, config=Config(signature_version='s3v4'))


def _load_doc_from_s3(bucket, key):
    auth_doc = None

    try:
        s3_client = _get_s3_client()
        auth_res = s3_client.get_object(Bucket=bucket, Key=key)
        auth_doc = auth_res['Body'].read()
    except ClientError as e:
        error_code = e.response['Error']['Code']
        if error_code == 'NoSuchKey' or error_code == 'AccessDenied':
            auth_doc = '{ }'

    return auth_doc
