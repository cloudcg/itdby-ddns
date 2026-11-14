#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import sys
import json
import time
import logging
import ipaddress
import requests
import configparser
import signal
from datetime import datetime
from logging.handlers import RotatingFileHandler

from huaweicloudsdkcore.auth.credentials import BasicCredentials
from huaweicloudsdkcore.exceptions import exceptions
from huaweicloudsdkdns.v2.dns_client import DnsClient
from huaweicloudsdkdns.v2.region.dns_region import DnsRegion
from huaweicloudsdkdns.v2.model.list_public_zones_request import ListPublicZonesRequest
from huaweicloudsdkdns.v2.model.list_record_sets_request import ListRecordSetsRequest
from huaweicloudsdkdns.v2.model.create_record_set_request import CreateRecordSetRequest
from huaweicloudsdkdns.v2.model.create_record_set_request_body import CreateRecordSetRequestBody
from huaweicloudsdkdns.v2.model.update_record_set_request import UpdateRecordSetRequest
from huaweicloudsdkdns.v2.model.update_record_set_req import UpdateRecordSetReq

VERSION = "v1.6"

config_file = './config/config.ini'
config = configparser.ConfigParser()
if not os.path.exists(config_file):
    os.makedirs(os.path.dirname(config_file), exist_ok=True)
    config['credentials'] = {'ak': 'your_access_key_id', 'sk': 'your_secret_access_key'}
    config['dns'] = {'region': 'af-south-1', 'ttl': '300'}
    config['domains'] = {'domain_list': '["example.com"]'}
    config['log'] = {'log_dir': './logs'}
    config['task'] = {'interval_seconds': '300', 'startup_check': 'true'}
    with open(config_file, 'w', encoding='utf-8') as f:
        config.write(f)
    print(f"已创建默认配置文件 {config_file}，请修改后重新运行")
    sys.exit(1)

config.read(config_file, encoding='utf-8')

log_dir = config.get('log', 'log_dir', fallback='./logs')
os.makedirs(log_dir, exist_ok=True)
log_file = os.path.join(log_dir, 'ddns.log')

formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger('ddns')
logger.setLevel(logging.INFO)
file_handler = RotatingFileHandler(log_file, maxBytes=5 * 1024 * 1024, backupCount=2, encoding='utf-8')
file_handler.setFormatter(formatter)
stream_handler = logging.StreamHandler()
stream_handler.setFormatter(formatter)
logger.addHandler(file_handler)
logger.addHandler(stream_handler)


class HuaweiDDNS:
    def __init__(self):
        self.ak = config.get('credentials', 'ak')
        self.sk = config.get('credentials', 'sk')
        self.ttl = int(config.get('dns', 'ttl', fallback='300'))
        self.domains = json.loads(config.get('domains', 'domain_list'))
        self.region = config.get('dns', 'region', fallback=None)
        self.endpoint = config.get('dns', 'endpoint', fallback=None)

        if not self.region and not self.endpoint:
            logger.error("配置文件缺少 dns 下的 region 或 endpoint")
            sys.exit(1)

        self.client = self._create_client()
        self.zone_map = {}

    def _create_client(self):
        credentials = BasicCredentials(self.ak, self.sk)
        if self.region:
            return DnsClient.new_builder().with_credentials(credentials).with_region(DnsRegion.value_of(self.region)).build()
        else:
            return DnsClient.new_builder().with_credentials(credentials).with_endpoint(self.endpoint).build()

    def get_public_ip(self):
        # 按国内可达性排序；单个源失败只记 DEBUG，全部失败才告警，避免刷屏。
        services = ["https://icanhazip.com", "https://ifconfig.me/ip", "https://checkip.amazonaws.com"]
        failures = []
        for s in services:
            try:
                resp = requests.get(s, timeout=(3, 5), headers={'User-Agent': 'curl/8.5.0'})
                if resp.status_code != 200:
                    failures.append(f"{s} HTTP {resp.status_code}")
                    continue
                ip = resp.text.strip()
                try:
                    addr = ipaddress.ip_address(ip)
                except ValueError:
                    failures.append(f"{s} 非 IP: {ip[:40]!r}")
                    continue
                if addr.version != 4:
                    failures.append(f"{s} 非 IPv4: {ip}")
                    continue
                logger.debug(f"从 {s} 获取公网IP: {ip}")
                return ip
            except Exception as e:
                failures.append(f"{s} {type(e).__name__}")
                logger.debug(f"从 {s} 获取IP失败: {e}")
        logger.warning(f"所有公网IP探测源均失败: {', '.join(failures)}")
        return None

    def get_zone_id(self, domain):
        if domain in self.zone_map:
            return self.zone_map[domain]
        try:
            req = ListPublicZonesRequest()
            req.name = domain
            resp = self.client.list_public_zones(req)
            zones = resp.zones
            if zones:
                zone_id = zones[0].id
                self.zone_map[domain] = zone_id
                logger.info(f"域名 {domain} 的 Zone ID: {zone_id}")
                return zone_id
            logger.error(f"未找到域名 {domain} 的 Zone ID")
            return None
        except exceptions.ClientRequestException as e:
            logger.error(f"获取域名 {domain} 的 Zone ID 失败: {e.status_code} {e.error_msg}")
            return None

    def get_record_sets(self, zone_id, name, record_type="A"):
        try:
            req = ListRecordSetsRequest()
            req.zone_id = zone_id
            req.name = name
            req.type = record_type
            resp = self.client.list_record_sets(req)
            # 华为云 name 过滤是模糊匹配（查 itdby.top. 会带出 *.itdby.top.），
            # 这里只保留精确匹配，避免把泛解析记录当成 apex 记录改掉。
            recordsets = resp.recordsets or []
            exact = [r for r in recordsets if r.name == name]
            if not exact and recordsets:
                logger.debug(f"记录集 {name} 无精确匹配，忽略: {[r.name for r in recordsets]}")
            return exact
        except exceptions.ClientRequestException as e:
            logger.error(f"获取记录集 {name} 失败: {e.status_code} {e.error_msg}")
            return []

    def create_record_set(self, zone_id, name, ip, record_type="A"):
        try:
            req = CreateRecordSetRequest()
            req.zone_id = zone_id
            req.body = CreateRecordSetRequestBody(name=name, type=record_type, ttl=self.ttl, records=[ip])
            self.client.create_record_set(req)
            logger.info(f"成功创建记录集: {name} -> {ip}")
            return True
        except exceptions.ClientRequestException as e:
            logger.error(f"创建记录集 {name} 失败: {e.status_code} {e.error_msg}")
            return False

    def update_record_set(self, zone_id, record_set_id, name, ip, record_type="A"):
        try:
            req = UpdateRecordSetRequest()
            req.zone_id = zone_id
            req.recordset_id = record_set_id
            req.body = UpdateRecordSetReq(name=name, type=record_type, ttl=self.ttl, records=[ip])
            self.client.update_record_set(req)
            logger.info(f"成功更新记录集: {name} -> {ip}")
            return True
        except exceptions.ClientRequestException as e:
            logger.error(f"更新记录集 {name} 失败: {e.status_code} {e.error_msg}")
            return False

    def update_dns_record(self, domain, record_name, ip):
        zone_id = self.get_zone_id(domain)
        if not zone_id:
            return False
        record_sets = self.get_record_sets(zone_id, record_name)
        if record_sets:
            record_set = record_sets[0]
            if record_set.records and record_set.records[0] == ip:
                return True
            return self.update_record_set(zone_id, record_set.id, record_name, ip)
        return self.create_record_set(zone_id, record_name, ip)

    def process_domain(self, domain, ip):
        # 保留 *. 前缀作为真实记录名；只有确定 zone 时才去掉它。
        wildcard = domain.startswith('*.')
        base = domain[2:] if wildcard else domain
        parts = base.split('.')
        main_domain = '.'.join(parts[-2:])
        record_name = f"*.{base}." if wildcard else f"{base}."
        return self.update_dns_record(main_domain, record_name, ip)

    def run(self, ip):
        logger.info(f"开始 DDNS 更新 - {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        results = {d: self.process_domain(d, ip) for d in self.domains}
        success = all(results.values())
        if success:
            logger.info(f"DDNS 更新成功: {', '.join(results)}")
        else:
            failed = [d for d, ok in results.items() if not ok]
            logger.error(f"DDNS 更新失败: {', '.join(failed)}")
        return success


def read_last_ip(path):
    try:
        with open(path, 'r', encoding='utf-8') as f:
            return f.read().strip() or None
    except OSError:
        return None


def write_last_ip(path, ip):
    tmp = f"{path}.tmp"
    with open(tmp, 'w', encoding='utf-8') as f:
        f.write(ip)
    os.replace(tmp, path)


def main():
    logger.info(f"itdby-ddns {VERSION} 启动")
    last_ip_file = "./config/last_ip.txt"
    last_ip = read_last_ip(last_ip_file)
    if last_ip:
        logger.info(f"读取上次公网IP: {last_ip}")

    interval = max(30, config.getint('task', 'interval_seconds', fallback=300))
    startup_check = config.getboolean('task', 'startup_check', fallback=True)
    check_on_startup = startup_check

    stopping = {'flag': False}

    def _stop(signum, frame):
        stopping['flag'] = True

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)

    ddns = HuaweiDDNS()
    while not stopping['flag']:
        try:
            current_ip = ddns.get_public_ip()
            if current_ip is None:
                logger.warning(f"本次未能获取公网IP，{interval} 秒后重试")
            elif check_on_startup or current_ip != last_ip:
                if current_ip != last_ip:
                    logger.info(f"检测到公网IP变化: {last_ip} -> {current_ip}")
                else:
                    logger.info(f"启动校验: 公网IP 与上次记录一致 ({current_ip})")
                if ddns.run(current_ip):
                    if current_ip != last_ip:
                        write_last_ip(last_ip_file, current_ip)
                    last_ip = current_ip
            else:
                logger.debug(f"公网IP未变化: {current_ip}")
            check_on_startup = False
        except Exception as e:
            logger.exception(f"本轮更新异常，{interval} 秒后重试: {e}")

        for _ in range(interval):
            if stopping['flag']:
                break
            time.sleep(1)

    logger.info("程序已停止")
    return 0


if __name__ == "__main__":
    sys.exit(main())
