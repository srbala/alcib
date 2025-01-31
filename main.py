# -*- mode:python; coding:utf-8; -*-
# author: Mariia Boldyreva <mboldyreva@cloudlinux.com>
# created: 2021-10-28

"""
Script for building Vagrant Boxes.
"""

import sys
import argparse
import logging
import os
import base64
import json
import requests

from lib.builder import Builder
from lib.hypervisors import get_hypervisor, TIMESTAMP
from lib.config import settings
from lib.utils import get_git_branches


headers = {
        'Authorization': f'Bearer {settings.github_token}',
        'Accept': 'application/vnd.github.v3+json',
    }


def init_args_parser() -> argparse.ArgumentParser:
    """
    Command line arguments parser initialization.

    Returns
    -------
    argparse.ArgumentParser
    """
    parser = argparse.ArgumentParser(
        description='Cloud Images autobuilder'
    )
    parser.add_argument('--hypervisor', type=str,
                        choices=['VirtualBox', 'KVM', 'VMWare_Desktop',
                                 'HyperV', 'AWS-STAGE-2', 'Equinix'],
                        help='Hypervisor name', required=False)
    parser.add_argument('--stage', type=str,
                        choices=['init', 'build', 'destroy',
                                 'test', 'release', 'pullrequest'],
                        help='Stage')
    parser.add_argument('--arch', type=str, choices=['x86_64', 'aarch64', 'ppc64le'],
                        help='Architecture', required=False, default='x86_64')
    return parser


def almalinux_wiki_pr():
    """
    Executes Github API calls for making commit and pull request.
    """
    repo = 'https://api.github.com/repos/AlmaLinux/wiki'
    response = requests.post(
        f'{repo}/merge-upstream',
        headers=headers, data='{"branch":"master"}'
    )
    logging.info(response.status_code, response.content.decode())

    aws_md = os.path.join(os.getcwd(), 'wiki/docs/cloud/AWS_AMIS.md')
    lines = open(aws_md, 'r').readlines()
    lines = lines[:1] + lines[3:]
    open(aws_md, 'w').write(''.join(lines))
    aws_csv = os.path.join(os.getcwd(), 'wiki/docs/.vuepress/public/ci-data/aws_amis.csv')
    md_content = base64.b64encode(
        open(aws_md, "r").read().encode('utf-8')
    ).decode('utf-8')
    csv_content = base64.b64encode(
        open(aws_csv, "r").read().encode('utf-8')
    ).decode('utf-8')
    response = requests.get(
        f'{repo}/contents/docs/cloud/AWS_AMIS.md',
        headers=headers
    )
    sha_md = json.loads(response.content.decode()).get('sha')
    response = requests.get(
        f'{repo}/contents/docs/.vuepress/public/ci-data/aws_amis.csv',
        headers=headers
    )
    sha_csv = json.loads(response.content.decode()).get('sha')
    data = f'{{"message":"Updating AWS AMI version in MD file",' \
           f'"content":"{md_content}","sha":"{sha_md}"}}'
    response = requests.put(
        f'{repo}/contents/docs/cloud/AWS_AMIS.md',
        headers=headers, data=data
    )
    logging.info(response.status_code, response.content.decode())

    data = f'{{"message":"Updating AWS AMI version in CSV file",' \
           f'"content":"{csv_content}","sha":"{sha_csv}"}}'
    response = requests.put(
        f'{repo}/contents/docs/.vuepress/public/ci-data/aws_amis.csv',
        headers=headers, data=data
    )
    logging.info(response.status_code, response.content.decode())

    data = '{"head":"AlmaLinux:master","base":"master","title":"Updating AWS AMI versions"}'
    response = requests.post(
        'https://api.github.com/repos/AlmaLinux/wiki/pulls',
        headers=headers, data=data
    )
    logging.info(response.status_code, response.content.decode())


def create_new_branch():
    repo = 'https://api.github.com/repos/AlmaLinux/docker-images'
    branches = get_git_branches(headers, repo)
    branch = branches[-1]
    if f'al-{settings.almalinux}-{TIMESTAMP}' not in branches:
        response = requests.get(f'{repo}/git/refs/heads', headers=headers)
        refs = json.loads(response.content.decode())
        sha_sum = None
        for ref in refs:
            if branch in ref['ref']:
                sha_sum = ref['object']['sha']
        data = {"ref": f"refs/heads/al-{settings.almalinux}-{TIMESTAMP}", "sha": sha_sum}
        response = requests.post(f'{repo}/git/refs', headers=headers, json=data)
        logging.info(response.content.decode())


def setup_logger():
    """
    Setup for logger.
    """
    handler = logging.StreamHandler()
    handler.setLevel(logging.INFO)
    log_format = "%(asctime)s %(levelname)-8s [%(threadName)s]: %(message)s"
    formatter = logging.Formatter(log_format, '%y.%m.%d %H:%M:%S')
    handler.setFormatter(formatter)

    logger = logging.getLogger()
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)


def main(sys_args):
    """
    Executes stages to build, test and release Vagrant Box.
    """
    args_parser = init_args_parser()
    args = args_parser.parse_args(sys_args)

    setup_logger()
    builder = Builder()

    if args.stage == 'pullrequest':
        almalinux_wiki_pr()
    else:
        hypervisor = get_hypervisor(args.hypervisor.lower(), args.arch)

        if args.stage == 'init':
            hypervisor.init_stage(builder)
        elif args.stage == 'build':
            if settings.image in ['Vagrant Box', 'GenericCloud', 'OpenNebula']:
                hypervisor.build_stage(builder)
            elif settings.image == 'AWS AMI' and args.hypervisor != 'AWS-STAGE-2':
                hypervisor.build_aws_stage(builder, args.arch)
            elif args.hypervisor == 'AWS-STAGE-2':
                hypervisor.init_stage(builder)
                hypervisor.build_aws_stage(builder, args.arch)
            elif settings.image == 'Docker':
                hypervisor.build_docker_stage(builder)
                create_new_branch()
                hypervisor.create_docker_branch(builder)
        elif args.stage == 'test':
            if settings.image == 'AWS AMI':
                hypervisor.test_aws_stage(builder)
            elif settings.image == 'GenericCloud':
                hypervisor.test_openstack(builder)
            else:
                hypervisor.test_stage(builder)
        elif args.stage == 'release':
            if settings.image in ['OpenNebula', 'GenericCloud']:
                hypervisor.release_and_sign_stage(builder)
            elif settings.image == 'AWS AMI':
                hypervisor.publish_ami(builder)
            else:
                hypervisor.release_stage(builder)
        elif args.stage == 'destroy':
            if settings.image in ['OpenNebula', 'GenericCloud'] and args.arch == 'aarch64':
                hypervisor.teardown_equinix_stage(builder)
            elif settings.image == 'Docker' and args.arch == 'ppc64le':
                hypervisor.clear_ppc64le_host(builder)
            else:
                hypervisor.teardown_stage()


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
