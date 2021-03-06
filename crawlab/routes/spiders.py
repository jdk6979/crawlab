import json
import os
import shutil
import subprocess
from datetime import datetime
from random import random

import requests
from bson import ObjectId
from flask import current_app, request
from flask_restful import reqparse, Resource
from werkzeug.datastructures import FileStorage

from config import PROJECT_DEPLOY_FILE_FOLDER, PROJECT_SOURCE_FILE_FOLDER, PROJECT_TMP_FOLDER
from constants.node import NodeStatus
from constants.task import TaskStatus
from db.manager import db_manager
from routes.base import BaseApi
from tasks.spider import execute_spider
from utils import jsonify
from utils.deploy import zip_file, unzip_file
from utils.file import get_file_suffix_stats, get_file_suffix
from utils.spider import get_lang_by_stats

parser = reqparse.RequestParser()
parser.add_argument('file', type=FileStorage, location='files')


class SpiderApi(BaseApi):
    col_name = 'spiders'

    arguments = (
        # name of spider
        ('name', str),

        # execute shell command
        ('cmd', str),

        # spider source folder
        ('src', str),

        # spider type
        ('type', str),

        # spider language
        ('lang', str),

        # spider results collection
        ('col', str),
    )

    def get(self, id=None, action=None):
        # action by id
        if action is not None:
            if not hasattr(self, action):
                return {
                           'status': 'ok',
                           'code': 400,
                           'error': 'action "%s" invalid' % action
                       }, 400
            return getattr(self, action)(id)

        # get one node
        elif id is not None:
            return jsonify(db_manager.get('spiders', id=id))

        # get a list of items
        else:
            items = []
            dirs = os.listdir(PROJECT_SOURCE_FILE_FOLDER)
            for _dir in dirs:
                dir_path = os.path.join(PROJECT_SOURCE_FILE_FOLDER, _dir)
                dir_name = _dir
                spider = db_manager.get_one_by_key('spiders', key='src', value=dir_path)

                # new spider
                if spider is None:
                    stats = get_file_suffix_stats(dir_path)
                    lang = get_lang_by_stats(stats)
                    db_manager.save('spiders', {
                        'name': dir_name,
                        'src': dir_path,
                        'lang': lang,
                        'suffix_stats': stats,
                    })

                # existing spider
                else:
                    stats = get_file_suffix_stats(dir_path)
                    lang = get_lang_by_stats(stats)
                    db_manager.update_one('spiders', id=str(spider['_id']), values={
                        'lang': lang,
                        'suffix_stats': stats,
                    })

                # append spider
                items.append(spider)

            return jsonify({
                'status': 'ok',
                'items': items
            })

    def crawl(self, id):
        args = self.parser.parse_args()
        node_id = args.get('node_id')

        if node_id is None:
            return {
                       'code': 400,
                       'status': 400,
                       'error': 'node_id cannot be empty'
                   }, 400

        # get node from db
        node = db_manager.get('nodes', id=node_id)

        # validate ip and port
        if node.get('ip') is None or node.get('port') is None:
            return {
                       'code': 400,
                       'status': 'ok',
                       'error': 'node ip and port should not be empty'
                   }, 400

        # dispatch crawl task
        res = requests.get('http://%s:%s/api/spiders/%s/on_crawl?node_id=%s' % (
            node.get('ip'),
            node.get('port'),
            id,
            node_id
        ))
        data = json.loads(res.content.decode('utf-8'))
        return {
            'code': res.status_code,
            'status': 'ok',
            'error': data.get('error'),
            'task': data.get('task')
        }

    def on_crawl(self, id):
        job = execute_spider.delay(id)

        return {
            'code': 200,
            'status': 'ok',
            'task': {
                'id': job.id,
                'status': job.status
            }
        }

    def deploy(self, id):
        spider = db_manager.get('spiders', id=id)
        nodes = db_manager.list('nodes', {})

        for node in nodes:
            node_id = node['_id']

            output_file_name = '%s_%s.zip' % (
                datetime.now().strftime('%Y%m%d%H%M%S'),
                str(random())[2:12]
            )
            output_file_path = os.path.join(PROJECT_TMP_FOLDER, output_file_name)

            # zip source folder to zip file
            zip_file(source_dir=spider['src'],
                     output_filename=output_file_path)

            # upload to api
            files = {'file': open(output_file_path, 'rb')}
            r = requests.post('http://%s:%s/api/spiders/%s/deploy_file?node_id=%s' % (
                node.get('ip'),
                node.get('port'),
                id,
                node_id,
            ), files=files)

        return {
            'code': 200,
            'status': 'ok',
            'message': 'deploy success'
        }

    def deploy_file(self, id=None):
        args = parser.parse_args()
        node_id = request.args.get('node_id')
        f = args.file

        if get_file_suffix(f.filename) != 'zip':
            return {
                       'status': 'ok',
                       'error': 'file type mismatch'
                   }, 400

        # save zip file on temp folder
        file_path = '%s/%s' % (PROJECT_TMP_FOLDER, f.filename)
        with open(file_path, 'wb') as fw:
            fw.write(f.stream.read())

        # unzip zip file
        dir_path = file_path.replace('.zip', '')
        if os.path.exists(dir_path):
            shutil.rmtree(dir_path)
        unzip_file(file_path, dir_path)

        # get spider and version
        spider = db_manager.get(col_name=self.col_name, id=id)
        if spider is None:
            return None, 400

        # make source / destination
        src = os.path.join(dir_path, os.listdir(dir_path)[0])
        # src = dir_path
        dst = os.path.join(PROJECT_DEPLOY_FILE_FOLDER, str(spider.get('_id')))

        # logging info
        current_app.logger.info('src: %s' % src)
        current_app.logger.info('dst: %s' % dst)

        # remove if the target folder exists
        if os.path.exists(dst):
            shutil.rmtree(dst)

        # copy from source to destination
        shutil.copytree(src=src, dst=dst)

        # save to db
        # TODO: task management for deployment
        db_manager.save('deploys', {
            'spider_id': ObjectId(id),
            'node_id': node_id,
            'finish_ts': datetime.now()
        })

        return {
            'code': 200,
            'status': 'ok',
            'message': 'deploy success'
        }

    def get_deploys(self, id):
        items = db_manager.list('deploys', cond={'spider_id': ObjectId(id)}, limit=10, sort_key='finish_ts')
        deploys = []
        for item in items:
            spider_id = item['spider_id']
            spider = db_manager.get('spiders', id=str(spider_id))
            item['spider_name'] = spider['name']
            deploys.append(item)
        return jsonify({
            'status': 'ok',
            'items': deploys
        })

    def get_tasks(self, id):
        items = db_manager.list('tasks', cond={'spider_id': ObjectId(id)}, limit=10, sort_key='finish_ts')
        for item in items:
            spider_id = item['spider_id']
            spider = db_manager.get('spiders', id=str(spider_id))
            item['spider_name'] = spider['name']
            task = db_manager.get('tasks_celery', id=item['_id'])
            if task is not None:
                item['status'] = task['status']
            else:
                item['status'] = TaskStatus.UNAVAILABLE
        return jsonify({
            'status': 'ok',
            'items': items
        })


class SpiderImportApi(Resource):
    parser = reqparse.RequestParser()
    arguments = [
        ('url', str)
    ]

    def __init__(self):
        super(SpiderImportApi).__init__()
        for arg, type in self.arguments:
            self.parser.add_argument(arg, type=type)

    def post(self, platform=None):
        if platform is None:
            return {
                       'status': 'ok',
                       'code': 404,
                       'error': 'platform invalid'
                   }, 404

        if not hasattr(self, platform):
            return {
                       'status': 'ok',
                       'code': 400,
                       'error': 'platform "%s" invalid' % platform
                   }, 400

        return getattr(self, platform)()

    def github(self):
        self._git()

    def gitlab(self):
        self._git()

    def _git(self):
        args = self.parser.parse_args()
        url = args.get('url')
        if url is None:
            return {
                       'status': 'ok',
                       'code': 400,
                       'error': 'url should not be empty'
                   }, 400

        try:
            p = subprocess.Popen(['git', 'clone', url], cwd=PROJECT_SOURCE_FILE_FOLDER)
            _stdout, _stderr = p.communicate()
        except Exception as err:
            return {
                       'status': 'ok',
                       'code': 500,
                       'error': str(err)
                   }, 500

        return {
            'status': 'ok',
            'message': 'success'
        }


class SpiderManageApi(Resource):
    parser = reqparse.RequestParser()
    arguments = [
        ('url', str)
    ]

    def post(self, action):
        if not hasattr(self, action):
            return {
                       'status': 'ok',
                       'code': 400,
                       'error': 'action "%s" invalid' % action
                   }, 400

        return getattr(self, action)()

    def deploy_all(self):
        # active nodes
        nodes = db_manager.list('nodes', {'status': NodeStatus.ONLINE})

        # all spiders
        spiders = db_manager.list('spiders', {'cmd': {'$exists': True}})

        # iterate all nodes
        for node in nodes:
            node_id = node['_id']
            for spider in spiders:
                spider_id = spider['_id']
                spider_src = spider['src']

                output_file_name = '%s_%s.zip' % (
                    datetime.now().strftime('%Y%m%d%H%M%S'),
                    str(random())[2:12]
                )
                output_file_path = os.path.join(PROJECT_TMP_FOLDER, output_file_name)

                # zip source folder to zip file
                zip_file(source_dir=spider_src,
                         output_filename=output_file_path)

                # upload to api
                files = {'file': open(output_file_path, 'rb')}
                r = requests.post('http://%s:%s/api/spiders/%s/deploy_file?node_id=%s' % (
                    node.get('ip'),
                    node.get('port'),
                    spider_id,
                    node_id,
                ), files=files)

        return {
            'status': 'ok',
            'message': 'success'
        }
