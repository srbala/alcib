# -*- mode:python; coding:utf-8; -*-
# author: Mariia Boldyreva <mboldyreva@cloudlinux.com>
# created: 2021-10-28

"""
Hypervisor's stages.
"""

import os
import json
import shutil
from datetime import datetime
import collections
from subprocess import PIPE, Popen, STDOUT
from io import BufferedReader, StringIO
import logging
import time
import re

import requests
import boto3
import ansible_runner

from lib.builder import Builder, ExecuteError
from lib.config import settings
from lib.utils import *


TIMESTAMP = str(datetime.date(datetime.today())).replace('-', '')
DT_SUFFIX = str(datetime.today()).replace('-', '').replace('.', '').replace(':', '').replace(' ', '_')
IMAGE = settings.image.replace(" ", "_")


class BaseHypervisor:
    """
    Basic configuration for any hypervisor.
    """

    cloud_images_path = '/home/ec2-user/cloud-images'
    sftp_path = '/home/ec2-user/cloud-images/'

    def __init__(self, name: str, arch: str):
        """
        Basic initialization.

        Parameters
        ----------
        name: str
            Hypervisor name.
        """
        self.name = name
        self.arch = arch
        self.os_major_ver = os.getenv('OS_MAJOR_VER')
        self._instance_ip = None
        self._instance_id = None
        self.build_number = settings.build_number
        self.s3_bucket = boto3.client(
            service_name='s3', region_name='us-east-1',
            aws_access_key_id=os.getenv('AWS_ACCESS_KEY_ID'),
            aws_secret_access_key=os.getenv('AWS_SECRET_ACCESS_KEY')
        )
        self.ec2_client = boto3.client(
            service_name='ec2', region_name='us-east-1',
            aws_access_key_id=os.getenv('AWS_ACCESS_KEY_ID'),
            aws_secret_access_key=os.getenv('AWS_SECRET_ACCESS_KEY')
        )

    @property
    def terraform_dir(self):
        """
        Gets location of terraform templates for a hypervisor.

        Returns
        -------
        Path
            Path to terraform templates.
        """
        if self.arch == 'aarch64':
            return os.path.join(os.getcwd(), 'terraform/{0}'.format(self.arch))
        return os.path.join(os.getcwd(), 'terraform/{0}'.format(self.name))

    def wait_instance_ready(self, ec2_ids):
        """
        Waits for EC2 instances to be ready for ssh connection.

        Parameters
        ----------
        ec2_ids: list
            List of EC2 instances ids.
        """
        logging.info('Checking if ready instances are ready...')
        waiter = self.ec2_client.get_waiter('instance_status_ok')
        waiter.wait(InstanceIds=ec2_ids)
        logging.info('Instances are ready')

    @property
    def instance_ip(self):
        """
        Gets AWS Instance public ip address.

        Returns
        -------
        str
            AWS Instance public ip address.
        """
        if not self._instance_ip:
            self.get_instance_info()
        return self._instance_ip

    @property
    def instance_id(self):
        """
        Gets AWS Instance id.

        Returns
        -------
        str
            AWS Instance id.
        """
        if not self._instance_id:
            self.get_instance_info()
        return self._instance_id

    def download_qcow(self) -> str:
        """
        Downloads qcow image from S3 bucket to jenkins node.

        Returns
        -------
        work_dir: str
            Working directory with downloaded image.
        """
        bucket_path = f'{self.build_number}-{IMAGE}-{self.name}-{self.arch}-{TIMESTAMP}'
        work_dir = os.path.join(os.getcwd(), f'{bucket_path}')
        os.mkdir(work_dir, mode=0o777)
        qcow_name = f'almalinux-{self.os_major_ver}-{settings.image}-{self.os_major_ver}.5'
        for i in range(5):
            try:
                self.s3_bucket.download_file(
                    settings.bucket,
                    f'{bucket_path}/{qcow_name}.{self.arch}.qcow2',
                    f'{work_dir}/{qcow_name}-{TIMESTAMP}.{self.arch}.qcow2'
                )
            except Exception as error:
                logging.exception('%s', error)
                time.sleep(60)
                if i == 4:
                    try:
                        execute_command(
                            f'aws s3 sync s3://{settings.bucket}/{bucket_path}/ '
                            f'{bucket_path}', os.getcwd()
                        )
                    except Exception as error:
                        logging.exception('%s', error)
                        raise error
        return work_dir

    def koji_release(self, ftp_path: str, qcow_name: str, builder: Builder):
        """
        Performs images release to koji.cloudlinux.com and
        repo-alma.corp.cloudlinux.com.

        Parameters
        ----------
        ftp_path: str
            FTP path to perform releases
        qcow_name: str
            Image's qcow full name.
        builder: Builder
            Main builder configuration.
        """
        ssh_koji = builder.ssh_remote_connect(
            settings.koji_ip, 'mockbuild', 'koji.cloudlinux.com'
        )
        deploy_path = f'deploy-repo-alma@{settings.alma_repo_ip}:/repo/almalinux/{self.os_major_ver}/cloud/'
        koji_commands = [
            f'ln -sf {ftp_path}/images/{qcow_name} '
            f'{ftp_path}/images/AlmaLinux-{self.os_major_ver}-{settings.image}-latest.{self.arch}.qcow2',
            f'sha256sum {ftp_path}/images/*.qcow2 > {ftp_path}/images/CHECKSUM',
        ]
        for cmd in koji_commands:
            try:
                stdout, _ = ssh_koji.safe_execute(cmd)
            except Exception as error:
                logging.exception(error)
        stdout, _ = ssh_koji.safe_execute(
            f"awk '$1=$1' ORS='\\n' {ftp_path}/images/CHECKSUM"
        )
        checksum_file = stdout.read().decode()
        headers = {
            'accept': 'application/json',
            'Authorization': f'Bearer {settings.sign_jwt_token}',
            'Content-Type': 'application/json',
        }
        json_data = {
            'content': f'{checksum_file}',
            'pgp_keyid': '488FCF7C3ABB34F8',
        }
        response = requests.post(
            'https://build.almalinux.org/api/v1/sign-tasks/sync_sign_task/',
            headers=headers, json=json_data)
        content = json.loads(response.content.decode())
        ssh_koji.upload_file(
            content["asc_content"], f'{ftp_path}/images/CHECKSUM.asc'
        )
        stdout, _ = ssh_koji.safe_execute(
            f'rsync -avSHP {ftp_path} {deploy_path}'
        )
        ssh_koji.close()
        ssh_deploy = builder.ssh_remote_connect(
            settings.alma_repo_ip, 'deploy-repo-alma',
            'repo-alma.corp.cloudlinux.com'
        )
        stdout, _ = ssh_deploy.safe_execute(
            'systemctl start --no-block rsync-repo-alma'
        )
        logging.info(stdout.read().decode())
        ssh_deploy.close()

    def get_instance_info(self):
        """
        Gets AWS Instance information for ssh connections.
        """
        output = Popen(['terraform', 'output', '--json'],
                       cwd=self.terraform_dir, stderr=STDOUT, stdout=PIPE)
        output_json = json.loads(BufferedReader(output.stdout).read().decode())
        self._instance_ip = output_json['instance_public_ip']['value']
        self._instance_id = output_json['instance_id']['value']

    def create_aws_instance(self):
        """
        Creates AWS Instance using Terraform commands.
        """
        if self.arch == 'aarch64':
            kvm_terraform = os.path.join(os.getcwd(), 'terraform/kvm')
            shutil.copytree(kvm_terraform, self.terraform_dir)
        logging.info('Creating AWS VM')
        terraform_commands = ['terraform init', 'terraform fmt',
                              'terraform validate']
        apply = 'terraform apply --auto-approve'
        if settings.image == 'Docker':
            if self.arch == 'aarch64':
                apply = 'terraform apply -var=ami_id=ami-070a38d61ee1ea697 ' \
                        '-var=instance_type=t4g.large --auto-approve'
            elif self.arch == 'x86_64':
                apply = 'terraform apply -var=ami_id=ami-0732b50c88bd647f2 ' \
                        '-var=instance_type=t3.medium --auto-approve'
        terraform_commands.append(apply)
        for cmd in terraform_commands:
            execute_command(cmd, self.terraform_dir)

    def teardown_stage(self):
        """
        Terminates AWS Instance.
        """
        logging.info('Destroying created VM')
        if os.path.exists(self.terraform_dir): 
            execute_command('terraform destroy --auto-approve', self.terraform_dir)
            if self.arch == 'aarch64':
                shutil.rmtree(self.terraform_dir)
        else:
            logging.info('Destroy VM alreaded completed')

    def upload_to_bucket(self, builder: Builder, files: list, file_path: str, ssh):
        """
        Upload files to S3 bucket.

        Parameters
        ----------
        builder : Builder
            Builder on AWS Instance.
        files : list
            List of files to upload to S3 bucket.
        file_path : str
            Path to files to upload.
        """
        logging.info('Uploading to S3 bucket')
        timestamp_name = f'{self.build_number}-{IMAGE}-{self.name}-{self.arch}-{TIMESTAMP}'
        for file in files:
            cmd = f'bash -c "sha256sum {file_path}/{file}"'
            try:
                stdout, _ = ssh.safe_execute(cmd)
            except ExecuteError:
                continue
            checksum = stdout.read().decode().split()[0]
            cmd = f'bash -c "aws s3 cp {file_path}/{file} ' \
                  f's3://{settings.bucket}/{timestamp_name}/ --metadata sha256={checksum}"'
            stdout, _ = ssh.safe_execute(cmd)
            logging.info(stdout.read().decode())
            logging.info('Uploaded')
        logging.info('Connection closed')

    def release_and_sign_stage(self, builder: Builder):
        """
        Signs and releases qcow2 image.

        Parameters
        ----------
        builder: Builder
            Main builder configuration.
        """
        qcow_name = f'AlmaLinux-{self.os_major_ver}-{settings.image}-{self.os_major_ver}-{TIMESTAMP}.{self.arch}.qcow2'
        ftp_path = f'/var/ftp/pub/cloudlinux/almalinux/{self.os_major_ver}/cloud/{self.arch}'
        qcow_path = self.download_qcow()
        execute_command(
            f'scp -i /var/lib/jenkins/.ssh/alcib_rsa4096 '
            f'{qcow_name} mockbuild@{settings.koji_ip}:{ftp_path}/images/{qcow_name}',
            qcow_path
        )
        try:
            self.koji_release(ftp_path, qcow_name, builder)
        finally:
            shutil.rmtree(qcow_path)

    def build_stage(self, builder: Builder):
        """
        Executes packer commands to build Vagrant Box.

        Parameters
        ----------
        builder : Builder
            Builder on AWS Instance.
        """
        ssh = builder.ssh_aws_connect(self.instance_ip, self.name)
        cmd2 = ""
        logging.info('Packer initialization')
        stdout, _ = ssh.safe_execute('packer init ./cloud-images 2>&1')
        logging.info(stdout.read().decode())
        logging.info('Building %s', settings.image)
        build_log = f'{IMAGE}_{self.arch}_build_{DT_SUFFIX}.log'
        build_log_2 = f'{IMAGE}_{self.arch}_build_{DT_SUFFIX}_2.log'
        if settings.image == 'GenericCloud':
            cmd = self.packer_build_gencloud.format(self.os_major_ver, build_log)
            cmd2 = self.packer_build_gencloud2.format(self.os_major_ver, build_log_2)
        elif settings.image == 'OpenNebula':
            if self.os_major_ver == '8':
                cmd = self.packer_build_opennebula.format(self.os_major_ver, build_log)
            else:
                cmd = self.packer_build_opennebula2.format(self.os_major_ver, build_log)
        else:
            cmd = self.packer_build_cmd.format(self.os_major_ver, build_log)
        try:
            stdout, _ = ssh.safe_execute(cmd)
            logging.info(stdout.read().decode())
            sftp_download(ssh, self.sftp_path, build_log, self.name)
            if settings.image == 'GenericCloud' and self.os_major_ver == '8' :
              stdout, _ = ssh.safe_execute(cmd2)
              logging.info(stdout.read().decode())
              sftp_download(ssh, self.sftp_path, build_log_2, self.name)
            logging.info('%s built', settings.image)
        finally:
            if settings.image == 'GenericCloud':
                file = f'output-almalinux-{self.os_major_ver}-gencloud-{self.arch}/*.qcow2'
                file2 = f'output-almalinux-{self.os_major_ver}-gencloud-uefi-{self.arch}/*.qcow2'
            elif settings.image == 'OpenNebula':
                file = f'output-almalinux-{self.os_major_ver}-opennebula-{self.arch}/*.qcow2'
            else:
                file = '*.box'
            files = [build_log, file]
            if settings.image == 'GenericCloud' and self.os_major_ver == '8' :
                files.append(build_log_2)
                files.append(file2)
            self.upload_to_bucket(
                builder, files,
                self.cloud_images_path, ssh
            )
        ssh.close()
        logging.info('Connection closed')

    def release_stage(self, builder: Builder):
        """
        Uploads vagrant box to Vagrant Cloud for the further release.

        Parameters
        ----------
        builder : Builder
            Builder on AWS Instance.
        """
        ssh = builder.ssh_aws_connect(self.instance_ip, self.name)
        logging.info('Creating new version for Vagrant Cloud')
        box = f'https://app.vagrantup.com/api/v1/box/{settings.vagrant}'
        version = os.environ.get('VERSION')
        stdout, _ = ssh.safe_execute(
            f'bash -c "sha256sum {self.cloud_images_path}/*.box"'
        )
        checksum = stdout.read().decode().split()[0]
        data = {'version': {'version': version,
                            'description': os.environ.get('CHANGELOG')}
                }
        get_headers = {
            'Authorization': f'Bearer {settings.vagrant_cloud_access_key}'
        }
        post_headers = {
            'Content-Type': 'application/json',
            'Authorization': f'Bearer {settings.vagrant_cloud_access_key}'
        }
        response = requests.get(
            f'{box}/version/{version}', headers=get_headers
        )
        if response.status_code == 404:
            response = requests.post(
                f'{box}/versions', headers=post_headers, data=json.dumps(data)
            )
            logging.info(response.content.decode())
        unchanged = ['virtualbox', 'vmware_desktop', 'hyperv']
        hypervisor = self.name if self.name in unchanged else 'libvirt'
        logging.info('Preparing for uploading')
        data = {
            'provider': {
                'name': hypervisor,
                'checksum_type': 'sha256',
                'checksum': checksum
            }
        }
        response = requests.post(
            f'{box}/version/{version}/providers',
            headers=post_headers, data=json.dumps(data)
        )
        logging.info(response.content.decode())

        response = requests.get(
            f'{box}/version/{version}/provider/{hypervisor}/upload',
            headers=get_headers
        )
        logging.info(response.content.decode())
        upload_path = json.loads(response.content.decode()).get('upload_path')
        logging.info('Uploading the box')
        stdout, _ = ssh.safe_execute(
            f'bash -c "curl {upload_path} --request PUT '
            f'--upload-file {self.cloud_images_path}/*.box"'
        )
        logging.info(stdout.read().decode())
        ssh.close()
        logging.info('Connection closed')

    def prepare_openstack(
            self, ssh, cloud_path: str, arch: str, test_path_tf: str
    ) -> str:
        """
        Prepares Openstack images for futher testing.
        """
        if self.os_major_ver == '9':
            logging.info('Dirty fix to terraform test script ...')
            stdout, _ = ssh.safe_execute(
                f'sed -i \'s/-8-GenericCloud-8.6/-9-GenericCloud-9.0/g\' {test_path_tf}/*/{arch}/*.tf 2>&1 && '
                f'sed -i \'s/AlmaLinux OS 8.6/AlmaLinux OS 9.0/g\' {test_path_tf}/*/{arch}/*.tf 2>&1'
            )
            logging.info(stdout.read().decode())
        logging.info('Uploading openstack image')
        stdout, _ = ssh.safe_execute(
            f'cp '
            f'{cloud_path}/output-almalinux-{self.os_major_ver}-gencloud-{self.arch}/*.qcow2 '
            f'{test_path_tf}/upload_image/{arch}/'
        )
        logging.info(stdout.read().decode())
        terraform_commands = ['terraform init', 'terraform fmt',
                              'terraform validate',
                              'terraform apply --auto-approve']
        for command in terraform_commands:
            stdout, _ = ssh.safe_execute(
                f'cd {test_path_tf}/upload_image/{arch}/ && {command}'
            )
            logging.info(stdout.read().decode())
        logging.info('Creating test instances')
        for command in terraform_commands:
            stdout, _ = ssh.safe_execute(
                f'cd {test_path_tf}/launch_test_instances/{arch}/ && {command}'
            )
            logging.info(stdout.read().decode())
        time.sleep(120)
        logging.info('Test instances are ready')
        logging.info('Starting testing')
        return f'genericcloud_test_{DT_SUFFIX}.log'


class LinuxHypervisors(BaseHypervisor):
    """
    Stages for Linux instances.
    """

    def init_stage(self, builder: Builder):
        """
        Creates and provisions AWS Instance.

        Parameters
        ----------
        builder : Builder
            Builder on AWS Instance.
        """
        if settings.image == 'Docker' and self.arch == 'ppc64le':
            lines = ['[aws_instance_public_ip]\n', settings.ppc64le_host, '\n']
            ansible_host = settings.ppc64le_host
            user = "alcib"
        else:
            self.create_aws_instance()
            self.wait_instance_ready([self.instance_id])
            lines = ['[aws_instance_public_ip]\n', self.instance_ip, '\n']
            ansible_host = self.instance_ip
            user = "ec2-user"
        inv = {
            "aws_instance": {
                "hosts": {
                    ansible_host: {
                        "ansible_user": user,
                        "ansible_ssh_private_key_file":
                            str(builder.AWS_KEY_PATH.absolute())
                    }
                }
            }
        }
        hosts_file = open('./ansible/hosts', 'w')
        hosts_file.writelines(lines)
        hosts_file.close()
        logging.info('Running Ansible')
        playbook = 'configure_aws_instance.yml'
        if settings.image == 'Docker':
            playbook = 'configure_docker.yml'
        ansible_runner.interface.run(project_dir='./ansible',
                                     playbook=playbook,
                                     inventory=inv)

    def test_stage(self, builder: Builder):
        """
        Runs testinfra tests for vagrant box and uploads its log to S3 bucket.

        Parameters
        ----------
        builder : Builder
            Builder on AWS Instance.
        """
        ssh = builder.ssh_aws_connect(self.instance_ip, self.name)
        logging.info('Preparing to test')
        stdout, _ = ssh.safe_execute(
            f'cd {self.cloud_images_path}/ '
            f'&& cp {self.cloud_images_path}/tests/vagrant/Vagrantfile . '
            f'&& export OS_MAJOR_VER={self.os_major_ver} '
            f'&& vagrant box add --name almalinux-{self.os_major_ver}-test *.box && vagrant up'
        )
        logging.info(stdout.read().decode())
        logging.info('Prepared for test')
        stdout, _ = ssh.safe_execute(
            f'cd {self.cloud_images_path}/ && '
            f'vagrant ssh-config > .vagrant/ssh-config'
        )
        logging.info(stdout.read().decode())
        logging.info('Starting testing')
        vb_test_log = f'vagrant_box_test_{DT_SUFFIX}.log'
        try:
            stdout, _ = ssh.safe_execute(
                f'cd {self.cloud_images_path}/ '
                f'&& py.test -v --hosts=almalinux-test-1,almalinux-test-2 '
                f'--ssh-config=.vagrant/ssh-config '
                f'{self.cloud_images_path}/tests/vagrant/test_vagrant.py '
                f'2>&1 | tee ./{vb_test_log}')
            logging.info(stdout.read().decode())
            sftp_download(ssh, self.cloud_images_path, vb_test_log, self.name)
            logging.info('Tested')
        finally:
            self.upload_to_bucket(
                builder, ['vagrant_box_test*.log'], self.cloud_images_path, ssh
            )
        ssh.close()
        logging.info('Connection closed')

    def build_docker_stage(self, builder: Builder):
        """
        Executes packer commands to build Vagrant Box.

        Parameters
        ----------
        builder : Builder
            Builder on AWS Instance.
        """
        if self.arch == 'ppc64le':
            user = 'alcib'
            ssh = builder.ssh_remote_connect(settings.ppc64le_host, user, 'PPC64LE')
        else:
            user = 'ec2-user'
            ssh = builder.ssh_aws_connect(self.instance_ip, self.name)
        docker_tmp = f'/home/{user}/docker-tmp/'
        docker_images = f'/home/{user}/docker-images/'
        docker_list = settings.docker_configuration.split(',')
        headers = {
            'Authorization': f'Bearer {settings.github_token}',
            'Accept': 'application/vnd.github.v3+json',
        }
        repo = 'https://api.github.com/repos/AlmaLinux/docker-images'
        response = requests.post(
            f'{repo}/merge-upstream',
            headers=headers, data='{"branch":"master"}'
        )
        logging.info(response.status_code, response.content.decode())
        stdout, _ = ssh.safe_execute(
            f'mkdir /home/{user}/.aws/ && mkdir {docker_tmp} && '
            f'sudo chown -R {user}:{user} {docker_tmp} && '
            f'sudo chown -R {user}:{user} {docker_images} && '
            f'sudo chown -R {user}:{user} /home/{user}/.aws/'
        )
        sftp = ssh.open_sftp()
        sftp.put(str(builder.AWS_KEY_PATH.absolute()), f'/home/{user}/aws_test')
        sftp.putfo(StringIO(builder.SSH_CONFIG), f'/home/{user}/.ssh/config')
        sftp.putfo(StringIO(builder.AWS_CREDENTIALS), f'/home/{user}/.aws/credentials')
        sftp.putfo(StringIO(builder.AWS_CONFIG), f'/home/{user}/.aws/config')
        logging.info('%s built', settings.image)
        repo = 'https://api.github.com/repos/AlmaLinux/docker-images'
        branches = get_git_branches(headers, repo)
        branch = branches[-1]
        requests.post(
            f'{repo}/merge-upstream',
            headers=headers, data='{"branch":"master"}'
        )
        stdout, _ = ssh.safe_execute(
            f'chmod 600 /home/{user}/.ssh/config && '
            f'chmod 600 /home/{user}/aws_test && '
            f'git clone git@github.com:AlmaLinux/docker-images.git {docker_tmp} && '
            f'cd {docker_tmp} && git checkout {branch} && '
            f'git config --global user.name "Mariia Boldyreva" && '
            f'git config --global user.email "shelterly@gmail.com"'
        )
        for conf in docker_list:
            stdout, _ = ssh.safe_execute(
                f'cd {docker_images} && git reset --hard && git checkout master && git pull'
            )
            build_log = f'{IMAGE}_{conf}_{self.arch}_build_{DT_SUFFIX}.log'
            try:
                stdout, _ = ssh.safe_execute(
                    f'cd {docker_images} && '
                    f'sudo ./build.sh -o {conf} -t {conf} 2>&1 | tee ./{build_log}'
                )
                sftp_download(
                    ssh, docker_images,
                    f'{conf}_{self.arch}-{conf}/logs/{build_log}', self.name
                )

                files = [
                    f'{conf}_{self.arch}-{conf}/logs/{IMAGE}_{conf}_{self.arch}_build*.log',
                    f'{conf}_{self.arch}-{conf}/Dockerfile-{self.arch}-{conf}',
                    f'{conf}_{self.arch}-{conf}/rpm-packages-{self.arch}-{conf}',
                    f'{conf}_{self.arch}-{conf}/almalinux-{self.os_major_ver}-docker-{self.arch}-{conf}.tar.xz'
                ]
                self.upload_to_bucket(builder, files, docker_images, ssh)
                for file in files:
                    stdout, _ = ssh.safe_execute(
                        f'cp {docker_images}{file} {docker_tmp}'
                    )
                stdout, _ = ssh.safe_execute(
                    f'mv {docker_tmp}rpm-packages-{self.arch}-{conf} '
                    f'{docker_tmp}rpm-packages-{conf}'
                )
            finally:
                logging.info(f'Docker Image {conf} built')
        ssh.close()
        logging.info('Connection closed')

    def create_docker_branch(self, builder):
        docker_list = settings.docker_configuration.split(',')
        text = [f'Updates AlmaLinux 8.5 {self.arch} {", ".join(docker_list)} rootfs']
        if self.arch == 'ppc64le':
            user = 'alcib'
            ssh = builder.ssh_remote_connect(settings.ppc64le_host, user, 'PPC64LE')
        else:
            user = 'ec2-user'
            ssh = builder.ssh_aws_connect(self.instance_ip, self.name)
        docker_tmp = '/home/{user}/docker-tmp/'
        if 'micro' in docker_list:
            docker_list.remove('micro')
        for conf in docker_list:
            stdout, _ = ssh.safe_execute(
                f"cd {docker_tmp} && git diff --unified=0 "
                f"{docker_tmp}rpm-packages-{conf} | grep '^[+|-][^+|-]' || true"
            )
            packages = stdout.read().decode()
            packages = packages.split('\n')
            stdout, _ = ssh.safe_execute(
                f'mkdir {docker_tmp}fake-root-{conf}/ && '
                f'sudo chown -R {user}:{user} {docker_tmp}fake-root-{conf}/ && '
                f'tar -xvf {docker_tmp}almalinux-{self.os_major_ver}-docker-{self.arch}-{conf}.tar.xz -C {docker_tmp}fake-root-{conf}'
            )
            raw_packages = list(filter(None, packages))
            packages = collections.defaultdict(dict)
            for raw_package in raw_packages:
                sign, raw_package = raw_package[0], raw_package[1:]
                package = parse_package(raw_package)
                packages[package.name][sign] = package
                if sign == '+':
                    changelog, _ = ssh.safe_execute(
                        f'sudo chroot {docker_tmp}fake-root-{conf}/ rpm -q --changelog {package.name}'
                    )
                    packages[package.name]['changelog'] = changelog.read().decode()
            for pkg in packages.values():
                if '+' not in pkg or '-' not in pkg:
                    continue
                added = pkg['+']
                removed = pkg['-']
                header = f'- {added.name} upgraded from {removed.version}-{removed.release} to {added.version}-{added.release}'
                cve_list = []
                for changelog_record in pkg['changelog'].split('\n\n'):
                    changelog_record = changelog_record.strip()
                    if not changelog_record:
                        continue
                    version_str = changelog_record.split('\n')[0].split()[-1]
                    # Remove epoch from version, since we don't know it for removed package
                    version_str = re.sub('^\d+:', '', version_str)
                    if f'{removed.version}-{removed.clean_release}' == version_str:
                        break
                    if f'{removed.version}-{removed.release}' == version_str:
                        break
                    if removed.version == version_str:
                        break
                    changelog_text = '\n'.join(changelog_record.split('\n')[1:])
                    cve_list.extend(re.findall(r'(CVE-[0-9]*-[0-9]*)', changelog_text))
                if cve_list:
                    header += f'\n  Fixes: {", ".join(cve_list)}'
                text.append(header)
        commit_msg = '\n\n'.join(text)

        stdout, _ = ssh.safe_execute(
            f'cd {docker_tmp} && git reset --hard && git fetch && '
            f'git checkout al-{settings.almalinux}-{TIMESTAMP} && git pull'
        )
        logging.info(stdout.read().decode())
        for conf in docker_list:
            stdout, _ = ssh.safe_execute(
                f'cp {docker_tmp}{conf}_{self.arch}-{conf}/Dockerfile-{self.arch}-{conf} {docker_tmp} && '
                f'cp {docker_tmp}{conf}_{self.arch}-{conf}/rpm-packages-{self.arch}-{conf} {docker_tmp}rpm-packages-{conf} && '
                f'cp {docker_tmp}{conf}_{self.arch}-{conf}/almalinux-{self.os_major_ver}-docker-{self.arch}-{conf}.tar.xz {docker_tmp}'
            )
        stdout, _ = ssh.safe_execute(
            f'cd /home/{user}/docker-tmp/ && '
            f'git add Dockerfile-{self.arch}* rpm-packages* *.tar.xz '
            f'&& git commit -m "{commit_msg}" && git push origin al-{settings.almalinux}-{TIMESTAMP}'
        )
        logging.info(stdout.read().decode())
        logging.info(commit_msg)

    @staticmethod
    def clear_ppc64le_host(self, builder):
        ssh = builder.ssh_remote_connect(settings.ppc64le_host, 'alcib', 'PPC64LE')
        cmd = 'sudo rm -rf /home/alcib/docker-images && sudo rm -rf /home/alcib/*-tmp && sudo rm -rf /home/alcib/.aws'
        stdout, _ = ssh.safe_execute(cmd)
        logging.info(stdout.read().decode())
        ssh.close()
        logging.info('Connection closed')


class HyperV(BaseHypervisor):
    """
    Stages specified for HyperV hypervisor.
    """
    cloud_images_path = '/mnt/c/Users/Administrator/cloud-images'
    sftp_path = 'c:\\Users\\Administrator\\cloud-images\\'
    packer_build_cmd = (
        'cd c:\\Users\\Administrator\\cloud-images ; '
        'packer build -var hyperv_switch_name=\"HyperV-vSwitch\" '
        '-only=\"hyperv-iso.almalinux-{}\" . '
        '| Tee-Object -file c:\\Users\\Administrator\\cloud-images\\{}'
    )

    def __init__(self, arch):
        """
        HyperV initialization.
        """
        super().__init__('hyperv', arch)

    def init_stage(self, builder: Builder):
        """
        Creates and provisions AWS Instance.

        Parameters
        ----------
        builder : Builder
            Builder on AWS Instance.
        """
        self.create_aws_instance()
        self.wait_instance_ready([self.instance_id])
        lines = ['[aws_instance_public_ip]\n', self.instance_ip, '\n']
        ansible_host = self.instance_ip
        user = "Administrator"
        inv = {
            "aws_instance": {
                "hosts": {
                    ansible_host: {
                        "ansible_user": user,
                        "ansible_connection": "ssh",
                        "ansible_shell_type": "powershell",
                        "ansible_ssh_private_key_file":
                            str(builder.AWS_KEY_PATH.absolute())
                    }
                }
            }
        }
        hosts_file = open('./ansible/hosts', 'w')
        hosts_file.writelines(lines)
        hosts_file.close()
        logging.info('Running Ansible')
        playbook = 'configure_aws_instance.yml'
        execute_command('ansible-galaxy install -r ./ansible/requirements.yaml', os.getcwd())
        ansible_runner.interface.run(project_dir='./ansible',
                                     playbook=playbook,
                                     inventory=inv)


    def test_stage(self, builder: Builder):
        """
        Runs testinfra tests for vagrant box and uploads its log to S3 bucket.

        Parameters
        ----------
        builder : Builder
            Builder on AWS Instance.
        """
        ssh = builder.ssh_aws_connect(self.instance_ip, self.name)
        logging.info('Preparing to test')
        cmd = "$Env:SMB_USERNAME = '{0}'; $Env:SMB_PASSWORD='{1}'; $Env:OS_MAJOR_VER={2}; " \
                "cd c:\\Users\\Administrator\\cloud-images\\ ; " \
                "cp c:\\Users\\Administrator\\cloud-images\\tests\\vagrant\\Vagrantfile . ; " \
                "vagrant box add --name almalinux-{2}-test *.box ; vagrant up".format(
                str(os.environ.get('WINDOWS_CREDS_USR')),
                str(os.environ.get('WINDOWS_CREDS_PSW')),
                str(self.os_major_ver)
                )
        stdout, _ = ssh.safe_execute(cmd)
        logging.info(stdout.read().decode())
        logging.info('Prepared for test')
        stdout, _ = ssh.safe_execute(
            f'cd {self.sftp_path} ; '
            f'vagrant ssh-config | Out-File -Encoding ascii -FilePath .vagrant/ssh-config'
        )
        logging.info(stdout.read().decode())
        logging.info('Starting testing')
        vb_test_log = f'vagrant_box_test_{DT_SUFFIX}.log'
        try:
            stdout, _ = ssh.safe_execute(
                f'cd {self.sftp_path} ; '
                f'py.test -v --hosts=almalinux-test-1,almalinux-test-2 '
                f'--ssh-config=.vagrant/ssh-config '
                f'{self.sftp_path}tests\\vagrant\\test_vagrant.py '
                f'| Out-File -FilePath {self.sftp_path}{vb_test_log}'
            )
            logging.info(stdout.read().decode())
            sftp_download(ssh, self.sftp_path, vb_test_log, self.name)
            logging.info('Tested')
        finally:
            self.upload_to_bucket(
                builder, [vb_test_log], self.cloud_images_path, ssh
            )
        ssh.close()
        logging.info('Connection closed')


class VirtualBox(LinuxHypervisors):
    """
    Specifies VirtualBox hypervisor.
    """
    packer_build_cmd = (
        'cd cloud-images && packer build -only=virtualbox-iso.almalinux-{} . '
        '2>&1 | tee ./{}'
    )

    def __init__(self, arch):
        """
        VirtualBox initialization.
        """
        super().__init__('virtualbox', arch)


class VMWareDesktop(LinuxHypervisors):
    """
    Specifies VMWare Desktop hypervisor.
    """
    packer_build_cmd = (
        'cd cloud-images && packer build -only=vmware-iso.almalinux-{} . '
        '2>&1 | tee ./{}'
    )

    def __init__(self, arch):
        """
        VMWare Desktop initialization.
        """
        super().__init__('vmware_desktop', arch)


class KVM(LinuxHypervisors):
    """
    Specifies KVM hypervisor.
    """
    packer_build_cmd = (
        "cd cloud-images && "
        "packer build -var qemu_binary='/usr/libexec/qemu-kvm' "
        "-only=qemu.almalinux-{} . 2>&1 | tee ./{}"
    )
    packer_build_gencloud = (
        "cd cloud-images && "
        "packer build -var qemu_binary='/usr/libexec/qemu-kvm'"
        " -var firmware_x86_64='/usr/share/edk2.git/ovmf-x64/OVMF_CODE-pure-efi.fd'"
        " -only=qemu.almalinux-{}-gencloud-x86_64 . 2>&1 | tee ./{}"
    )

    packer_build_gencloud2 = (
        "cd cloud-images && "
        "packer build -var qemu_binary='/usr/libexec/qemu-kvm'"
        " -var firmware_x86_64='/usr/share/edk2.git/ovmf-x64/OVMF_CODE-pure-efi.fd'"
        " -only=qemu.almalinux-{}-gencloud-uefi-x86_64 . 2>&1 | tee ./{}"
    )

    packer_build_opennebula = (
        "cd cloud-images && "
        "packer build -var qemu_binary='/usr/libexec/qemu-kvm' "
        "-only=qemu.almalinux-{}-opennebula-x86_64 . 2>&1 | tee ./{}"
    )
    packer_build_opennebula2 = (
        "cd cloud-images && "
        "packer build -var qemu_binary='/usr/libexec/qemu-kvm' "
        "-var firmware_x86_64='/usr/share/edk2.git/ovmf-x64/OVMF_CODE-pure-efi.fd' "
        "-only=qemu.almalinux-{}-opennebula-x86_64 . 2>&1 | tee ./{}"
    )

    def __init__(self, name='kvm', arch='x86_64'):
        """
        KVM initialization.
        """
        super().__init__(name, arch)

    def publish_ami(self, builder: Builder):
        """
        Prepare AMI files for publishing.
        """
        with open(f'ami_id_{self.arch}.txt', 'r') as ami_file:
            ami_id = ami_file.read()
        ssh = builder.ssh_aws_connect(self.instance_ip, self.name)
        logging.info('Preparing csv and md')
        cmd = "cd {}/ && " \
              "export AWS_DEFAULT_REGION='us-east-1' && " \
              "export AWS_ACCESS_KEY_ID='{}' " \
              "&& export AWS_SECRET_ACCESS_KEY='{}' " \
              "&& {}/bin/aws_ami_mirror.py " \
              "-a {} --csv-output aws_amis-{}.csv " \
              "--md-output AWS_AMIS-{}.md --verbose".format(
                  self.cloud_images_path,
                  os.getenv('AWS_ACCESS_KEY_ID'),
                  os.getenv('AWS_SECRET_ACCESS_KEY'),
                  self.cloud_images_path,
                  ami_id, self.arch, self.arch)
        stdout, _ = ssh.safe_execute(cmd)
        logging.info(stdout.read().decode())
        self.upload_to_bucket(
            builder,
            [f'aws_amis-{self.arch}.csv', f'AWS_AMIS-{self.arch}.md'],
            self.cloud_images_path, ssh
        )
        sftp_download(ssh, self.sftp_path, f'aws_amis-{self.arch}.csv', self.name)
        sftp_download(ssh, self.sftp_path, f'AWS_AMIS-{self.arch}.md', self.name)
        ssh.close()

    def build_aws_stage(self, builder: Builder, arch: str):
        """
        Builds new AWS EC2 instance.

        Parameters
        ----------
        builder: Builder
            Main AWS builder configuration.
        arch: str
            Architecture to build.
        """
        ssh = builder.ssh_aws_connect(self.instance_ip, self.name)
        logging.info('Packer initialization')
        stdout, _ = ssh.safe_execute('packer init ./cloud-images 2>&1')
        logging.info(stdout.read().decode())
        logging.info('Building AWS AMI')
        aws_build_log = f'aws_ami_build_{self.arch}_{DT_SUFFIX}.log'
        if self.os_major_ver == '8':
            if arch == 'x86_64':
                logging.info('Building Stage 1')
                cmd = "cd cloud-images && " \
                    "export AWS_DEFAULT_REGION='us-east-1' && " \
                    "export AWS_ACCESS_KEY_ID='{}' && " \
                    "export AWS_SECRET_ACCESS_KEY='{}' " \
                    "&& packer build -var aws_s3_bucket_name='{}' " \
                    "-var qemu_binary='/usr/libexec/qemu-kvm' " \
                    "-var aws_role_name='alma-images-prod-role' " \
                    "-only=qemu.almalinux-{}-aws-stage1 . 2>&1 | tee ./{}".format(
                        os.getenv('AWS_ACCESS_KEY_ID'),
                        os.getenv('AWS_SECRET_ACCESS_KEY'),
                        settings.bucket, self.os_major_ver, aws_build_log)
            else:
                cmd = "cd cloud-images && " \
                    "export AWS_DEFAULT_REGION='us-east-1' && " \
                    "export AWS_ACCESS_KEY_ID='{}' && " \
                    "export AWS_SECRET_ACCESS_KEY='{}' " \
                    "&& packer build " \
                    "-only=amazon-ebssurrogate.almalinux-{}-aws-aarch64 . " \
                    "2>&1 | tee ./{}".format(
                        os.getenv('AWS_ACCESS_KEY_ID'),
                        os.getenv('AWS_SECRET_ACCESS_KEY'),
                        self.os_major_ver, aws_build_log)
        else:
            cmd = "cd cloud-images && " \
                "export AWS_DEFAULT_REGION='us-east-1' && " \
                "export AWS_ACCESS_KEY_ID='{}' && " \
                "export AWS_SECRET_ACCESS_KEY='{}' " \
                "&& packer build " \
                "-only=amazon-ebssurrogate.almalinux-{}-ami-{} . " \
                "2>&1 | tee ./{}".format(
                    os.getenv('AWS_ACCESS_KEY_ID'),
                    os.getenv('AWS_SECRET_ACCESS_KEY'),
                    self.os_major_ver, arch, aws_build_log)
        try:
            stdout, _ = ssh.safe_execute(cmd)
        finally:
            self.upload_to_bucket(
                builder, [aws_build_log], self.cloud_images_path, ssh
            )
        sftp_download(ssh, self.sftp_path, aws_build_log, self.name)
        ami = save_ami_id(stdout.read().decode(), self.arch)
        aws_hypervisor = AwsStage2(self.arch)
        tfvars = {'ami_id': ami}
        tf_vars_file = os.path.join(aws_hypervisor.terraform_dir,
                                    'terraform.tfvars.json')
        with open(tf_vars_file, 'w') as tf_file_fd:
            json.dump(tfvars, tf_file_fd)
        sftp = ssh.open_sftp()
        sftp.get(
            os.path.join(self.cloud_images_path,
                         'build-tools-on-ec2-userdata.yml'),
            os.path.join(aws_hypervisor.terraform_dir,
                         'build-tools-on-ec2-userdata.yml')
        )
        ssh.close()
        logging.info('Connection closed')

    def test_aws_stage(self, builder: Builder):
        """
        builder: Builder
            Main builder configuration.

        Runs Testinfra tests for AWS AMI.
        """
        ssh = builder.ssh_aws_connect(self.instance_ip, self.name)
        sftp = ssh.open_sftp()
        sftp.put(str(builder.AWS_KEY_PATH.absolute()),
                 '/home/ec2-user/.ssh/alcib_rsa4096')
        ssh.safe_execute(
            'sudo chmod 700 /home/ec2-user/.ssh && '
            'sudo chmod 600 /home/ec2-user/.ssh/alcib_rsa4096'
        )
        arch = self.arch if self.arch == 'aarch64' else 'amd64'
        test_path_tf = f'{self.cloud_images_path}/tests/ami/launch_test_instances/{arch}'
        if self.os_major_ver == '9':
            logging.info('Dirty fix to terraform test script ...')
            stdout, _ = ssh.safe_execute(
                f'sed -i \'s/AlmaLinux OS 8./AlmaLinux OS 9./g\' {test_path_tf}/*.tf 2>&1'
            )
            logging.info(stdout.read().decode())

        logging.info('Creating test instances')
        stdout, _ = ssh.safe_execute(
            'mkdir /home/ec2-user/.aws/ && '
            'sudo chown -R ec2-user:ec2-user /home/ec2-user/.aws/'
        )
        sftp = ssh.open_sftp()
        sftp.putfo(StringIO(builder.AWS_CREDENTIALS), '/home/ec2-user/.aws/credentials')
        sftp.putfo(StringIO(builder.AWS_CONFIG), '/home/ec2-user/.aws/config')
        cmd_export = \
            "export AWS_DEFAULT_REGION='us-east-1' && " \
            "export AWS_ACCESS_KEY_ID='{}' && " \
            "export AWS_SECRET_ACCESS_KEY='{}'".format(
                os.getenv('AWS_ACCESS_KEY_ID'),
                os.getenv('AWS_SECRET_ACCESS_KEY'))
        terraform_commands = ['terraform init', 'terraform fmt',
                              'terraform validate',
                              f'{cmd_export} && terraform plan && terraform apply --auto-approve']
        for command in terraform_commands:
            stdout, _ = ssh.safe_execute(
                f'cd {test_path_tf} && {command}'
            )
            logging.info(stdout.read().decode())
        logging.info('Checking if test instances are ready')
        stdout, _ = ssh.safe_execute(
            f'cd {test_path_tf} && {cmd_export} && terraform output --json'
        )
        output = stdout.read().decode()
        logging.info(output)
        output_json = json.loads(output)
        self.wait_instance_ready([output_json['instance_id1']['value'],
                                 output_json['instance_id2']['value']])
        logging.info('Starting testing')
        aws_test_log = f'aws_ami_test_{DT_SUFFIX}.log'
        try:
            stdout, _ = ssh.safe_execute(
                f'cd {self.cloud_images_path} && '
                f'py.test -v --hosts=almalinux-test-1,almalinux-test-2 '
                f'--ssh-config={test_path_tf}/ssh-config '
                f'{self.cloud_images_path}/tests/ami/test_ami.py '
                f'2>&1 | tee ./{aws_test_log}'
            )
            logging.info(stdout.read().decode())
        finally:
            self.upload_to_bucket(
                builder, ['aws_ami_test*.log'], self.cloud_images_path, ssh
            )
        sftp_download(ssh, self.cloud_images_path, aws_test_log, self.arch)
        logging.info('Tested')
        stdout, _ = ssh.safe_execute(
            f'cd {test_path_tf} && {cmd_export} && '
            f'terraform destroy --auto-approve'
        )
        logging.info(stdout.read().decode())
        ssh.close()
        logging.info('Connection closed')

    def test_openstack(self, builder: Builder):
        """
        builder: Builder
            Main Builder Configuration.

        Runs Testinfra tests for the built openstack image.
        """
        yaml = os.path.join(os.getcwd(), 'clouds.yaml.j2')
        content = open(yaml, 'r').read()
        yaml_content = generate_clouds(content)
        ssh = builder.ssh_aws_connect(self.instance_ip, self.name)
        sftp = ssh.open_sftp()
        stdout, _ = ssh.safe_execute('mkdir -p /home/ec2-user/.config/openstack/')
        sftp.put(str(builder.AWS_KEY_PATH.absolute()),
                 '/home/ec2-user/.ssh/alcib_rsa4096')
        stdout, _ = ssh.safe_execute(
            'sudo chmod 700 /home/ec2-user/.ssh && '
            'sudo chmod 600 /home/ec2-user/.ssh/alcib_rsa4096'
        )
        yaml_file = sftp.file('/home/ec2-user/.config/openstack/clouds.yaml', "w")
        yaml_file.write(yaml_content)
        yaml_file.flush()
        arch = self.arch if self.arch == 'aarch64' else 'amd64'
        test_path_tf = f'{self.cloud_images_path}/tests/genericcloud'
        gc_test_log = self.prepare_openstack(
            ssh, '/home/ec2-user/cloud-images', arch, test_path_tf
        )
        script = f'{test_path_tf}/launch_test_instances/{arch}/test_genericcloud.py'
        cmd = f'cd {self.cloud_images_path} && ' \
              f'py.test -v --hosts=almalinux-test-1,almalinux-test-2 ' \
              f'--ssh-config={test_path_tf}/launch_test_instances/{arch}/ssh-config ' \
              f'{script} 2>&1 | tee ./{gc_test_log}'
        try:
            stdout, _ = ssh.safe_execute(cmd)
            logging.info(stdout.read().decode())
        finally:
            self.upload_to_bucket(
                builder, ['genericcloud_test*.log'], self.cloud_images_path, ssh
            )
            sftp_download(ssh, self.cloud_images_path, gc_test_log, self.arch)
            logging.info('Tested')
            stdout, _ = ssh.safe_execute(
                f'cd {test_path_tf}/launch_test_instances/{arch}/ && '
                f'terraform destroy --auto-approve'
            )
            logging.info(stdout.read().decode())
            stdout, _ = ssh.safe_execute(
                f'cd {test_path_tf}/upload_image/{arch}/ && '
                f'terraform destroy --auto-approve'
            )
            logging.info(stdout.read().decode())
        ssh.close()
        logging.info('Connection closed')


class AwsStage2(KVM):
    """
    AWS Stage 2 for building x86_64 AWS AMI.
    """

    def __init__(self, arch):
        super().__init__('aws-stage-2', arch)

    def build_aws_stage(self, builder: Builder, arch: str):
        ssh = builder.ssh_aws_connect(self.instance_ip, self.name)
        logging.info('Packer initialization')
        stdout, _ = ssh.safe_execute(
            f'cd {self.cloud_images_path} && sudo packer.io init .'
        )
        logging.info(stdout.read().decode())
        logging.info('Building AWS AMI')
        aws2_build_log = f'aws_ami_stage2_build_{DT_SUFFIX}.log'
        try:
            stdout, _ = ssh.safe_execute(
                'cd cloud-images && sudo AWS_ACCESS_KEY_ID="{}" '
                'AWS_SECRET_ACCESS_KEY="{}" AWS_DEFAULT_REGION="us-east-1" '
                'packer.io build -only=amazon-chroot.almalinux-{}-aws-stage2 '
                '. 2>&1 | tee ./{}'.format(
                    os.getenv('AWS_ACCESS_KEY_ID'),
                    os.getenv('AWS_SECRET_ACCESS_KEY'),
                    self.os_major_ver,
                    aws2_build_log
                )
            )
            output = stdout.read().decode()
            logging.info(output)
            save_ami_id(output, self.arch)
        finally:
            pass
        cmd = f'bash -c "sha256sum {self.cloud_images_path}/{aws2_build_log}"'
        stdout, _ = ssh.safe_execute(cmd)
        sftp_download(ssh, self.sftp_path, aws2_build_log, self.name)
        try:
            self.s3_bucket.upload_file(
                f'{self.name}-{aws2_build_log}', settings.bucket,
                f'{self.build_number}-{IMAGE}-{self.name}-{self.arch}-{TIMESTAMP}/{aws2_build_log}')
        except Exception as error:
            logging.exception('%s', error)
        ssh.close()
        logging.info('Connection closed')


class Equinix(BaseHypervisor):
    """
    Equnix Server for building and testing images.
    """

    packer_build_opennebula = (
        "cd cloud-images && "
        "packer.io build -var qemu_binary='/usr/libexec/qemu-kvm' "
        "-only=qemu.almalinux-{}-opennebula-aarch64 . 2>&1 | tee ./{}"
    )

    packer_build_gencloud = (
        "cd /root/cloud-images && "
        "packer.io build -var qemu_binary='/usr/libexec/qemu-kvm' "
        "-only=qemu.almalinux-{}-gencloud-aarch64 . 2>&1 | tee ./{}"
    )

    def __init__(self, name='equinix', arch='aarch64'):
        """
        KVM initialization.
        """
        super().__init__(name, arch)

    @staticmethod
    def init_stage(builder: Builder):
        """
        Makes initialization of Equinix Server for the image building.

        builder: Builder
            Main Builder Configuration.
        """
        ssh = builder.ssh_remote_connect(settings.equinix_ip, 'root', 'Equinix')
        logging.info('Connection is good')
        stdout, _ = ssh.safe_execute(
            'git clone https://github.com/AlmaLinux/cloud-images.git'
        )
        logging.info(stdout.read().decode())
        ssh.close()
        logging.info('Connection closed')

    def build_stage(self, builder: Builder):
        ssh = builder.ssh_remote_connect(settings.equinix_ip, 'root', 'Equinix')
        logging.info('Packer initialization')
        stdout, _ = ssh.safe_execute('packer.io init /root/cloud-images 2>&1')
        logging.info(stdout.read().decode())
        gc_build_log = f'{IMAGE}_{self.arch}_build_{DT_SUFFIX}.log'
        logging.info('Building %s', settings.image)
        if settings.image == 'GenericCloud':
            cmd = self.packer_build_gencloud.format(self.os_major_ver, gc_build_log)
        else:
            cmd = self.packer_build_opennebula.format(self.os_major_ver, gc_build_log)
        try:
            stdout, _ = ssh.safe_execute(cmd)
        finally:
            if settings.image == 'GenericCloud':
                file = 'output-almalinux-{}-gencloud-aarch64/*.qcow2'.format(self.os_major_ver)
            else:
                file = 'output-almalinux-{}-opennebula-aarch64/*.qcow2'.format(self.os_major_ver)
            self.upload_to_bucket(
                builder, [f'{IMAGE}_{self.arch}_build*.log', file],
                '/root/cloud-images/', ssh
            )
        sftp_download(ssh, '/root/cloud-images/', gc_build_log, self.name)
        logging.info('%s built', settings.image)
        ssh.close()
        logging.info('Connection closed')

    def test_openstack(self, builder: Builder):
        """
        builder: Builder
            Main Builder Configuration.

        Run Testinfra tests for the built openstack image.
        """
        yaml = os.path.join(os.getcwd(), 'clouds.yaml.j2')
        content = open(yaml, 'r').read()
        yaml_content = generate_clouds(content)
        ssh = builder.ssh_remote_connect(settings.equinix_ip, 'root', 'Equinix')
        sftp = ssh.open_sftp()
        sftp.put(str(builder.AWS_KEY_PATH.absolute()),
                 '/root/.ssh/alcib_rsa4096')
        stdout, _ = ssh.safe_execute('sudo chmod 700 /root/.ssh && '
                                     'sudo chmod 600 /root/.ssh/alcib_rsa4096')
        stdout, _ = ssh.safe_execute('mkdir -p /root/.config/openstack/')
        yaml_file = sftp.file('/root/.config/openstack/clouds.yaml', "w")
        yaml_file.write(yaml_content)
        yaml_file.flush()

        arch = self.arch if self.arch == 'aarch64' else 'amd64'
        test_path_tf = '/root/cloud-images/tests/genericcloud/'
        gc_test_log = self.prepare_openstack(
            ssh, '/root/cloud-images', arch, test_path_tf
        )
        script = f'{test_path_tf}/launch_test_instances/{arch}/test_genericcloud.py'
        try:
            stdout, _ = ssh.safe_execute(
                f'cd /root/cloud-images && '
                f'py.test -v --hosts=almalinux-test-1,almalinux-test-2 '
                f'--ssh-config={test_path_tf}/launch_test_instances/{arch}/ssh-config '
                f'{script} 2>&1 | tee ./{gc_test_log}')
            logging.info(stdout.read().decode())
        finally:
            self.upload_to_bucket(builder, [gc_test_log], '/root/cloud-images/', ssh)
            sftp_download(ssh, '/root/cloud-images/', gc_test_log, self.arch)
            logging.info('Tested')
            stdout, _ = ssh.safe_execute(
                f'cd {test_path_tf}/launch_test_instances/{arch}/ && '
                f'terraform destroy --auto-approve'
            )
            stdout, _ = ssh.safe_execute(
                f'cd {test_path_tf}/upload_image/{arch}/ && '
                f'terraform destroy --auto-approve'
            )
        ssh.close()
        logging.info('Connection closed')

    @staticmethod
    def teardown_equinix_stage(builder: Builder):
        """
        builder: Builder
            Main Builder Configuration.

        Cleans up Equinix Server.
        """
        ssh = builder.ssh_remote_connect(settings.equinix_ip, 'root', 'Equinix')
        cmd = 'sudo rm -rf /root/cloud-images/'
        stdout, _ = ssh.safe_execute(cmd)
        logging.info(stdout.read().decode())
        ssh.close()
        logging.info('Connection closed')


def get_hypervisor(hypervisor_name, arch='x86_64'):
    """
    Gets specified hypervisor to build a vagrant box.

    Parameters
    ----------
    hypervisor_name: str
        Hypervisor's name.
    arch: str
        Architecture

    Returns
    -------
    Specified Hypervisor.
    """
    return {
        'hyperv': HyperV,
        'virtualbox': VirtualBox,
        'kvm': KVM,
        'vmware_desktop': VMWareDesktop,
        'aws-stage-2': AwsStage2,
        'equinix': Equinix
    }[hypervisor_name](arch=arch)
