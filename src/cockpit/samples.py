# This file is part of Cockpit.
#
# Copyright (C) 2022 Red Hat, Inc.
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <https://www.gnu.org/licenses/>.

import os
import os.path
import sys
from typing import Any, Dict, List, NamedTuple, Optional


USER_HZ = os.sysconf(os.sysconf_names['SC_CLK_TCK'])
MS_PER_JIFFY = 1000 / (USER_HZ if (USER_HZ > 0) else 100)


class SampleDescription(NamedTuple):
    name: str
    units: str
    semantics: str
    instanced: bool


class Sampler:
    descriptions: List[SampleDescription]

    def sample(self, samples):
        raise NotImplementedError


class CPUSampler(Sampler):
    descriptions = [
        SampleDescription('cpu.basic.nice', 'millisec', 'counter', False),
        SampleDescription('cpu.basic.user', 'millisec', 'counter', False),
        SampleDescription('cpu.basic.system', 'millisec', 'counter', False),
        SampleDescription('cpu.basic.iowait', 'millisec', 'counter', False),

        SampleDescription('cpu.core.nice', 'millisec', 'counter', True),
        SampleDescription('cpu.core.user', 'millisec', 'counter', True),
        SampleDescription('cpu.core.system', 'millisec', 'counter', True),
        SampleDescription('cpu.core.iowait', 'millisec', 'counter', True),
    ]

    def sample(self, samples: Dict[str, Any]):
        with open('/proc/stat') as stat:
            for line in stat:
                if not line.startswith('cpu'):
                    continue
                cpu, user, nice, system, _idle, iowait = line.split()[:6]
                core = cpu[3:] or None
                if core:
                    prefix = 'cpu.core'
                    samples[f'{prefix}.nice'][core] = int(nice) * MS_PER_JIFFY
                    samples[f'{prefix}.user'][core] = int(user) * MS_PER_JIFFY
                    samples[f'{prefix}.system'][core] = int(system) * MS_PER_JIFFY
                    samples[f'{prefix}.iowait'][core] = int(iowait) * MS_PER_JIFFY
                else:
                    prefix = 'cpu.basic'
                    samples[f'{prefix}.nice'] = int(nice) * MS_PER_JIFFY
                    samples[f'{prefix}.user'] = int(user) * MS_PER_JIFFY
                    samples[f'{prefix}.system'] = int(system) * MS_PER_JIFFY
                    samples[f'{prefix}.iowait'] = int(iowait) * MS_PER_JIFFY


class MemorySampler(Sampler):
    descriptions = [
        SampleDescription('memory.free', 'bytes', 'instant', False),
        SampleDescription('memory.used', 'bytes', 'instant', False),
        SampleDescription('memory.cached', 'bytes', 'instant', False),
        SampleDescription('memory.swap-used', 'bytes', 'instant', False),
    ]

    def sample(self, samples: Dict[str, Any]):
        with open('/proc/meminfo') as meminfo:
            items = {k: int(v.strip(' kB\n')) for line in meminfo for k, v in [line.split(':', 1)]}

        samples['memory.free'] = 1024 * items['MemFree']
        samples['memory.used'] = 1024 * (items['MemTotal'] - items['MemAvailable'])
        samples['memory.cached'] = 1024 * (items['Buffers'] + items['Cached'])
        samples['memory.swap-used'] = 1024 * (items['SwapTotal'] - items['SwapFree'])


class CPUTemperatureSampler(Sampler):
    # Cache found sensors, as they can't be hotplugged.
    sensors: List[str] = []

    descriptions = [
        SampleDescription('cpu.temperature', 'celsius', 'instant', True),
    ]

    def detect_cpu_sensors(self, hwmonid: int, name: str):
        for index in range(1, 2 ** 32):
            sensor_path = f'/sys/class/hwmon/hwmon{hwmonid}/temp{index}_input'
            if not os.path.exists(sensor_path):
                break

            label = open(f'/sys/class/hwmon/hwmon{hwmonid}/temp{index}_label').read().strip()
            if label:
                # only sample CPU Temperature in atk0110
                if label != 'CPU Temperature' and name == 'atk0110':
                    continue
                # ignore Tctl on AMD devices
                if label == 'Tctl':
                    continue
            else:
                # labels are not used on ARM
                if name != 'cpu_thermal':
                    continue

            self.sensors.append(sensor_path)

    def sample(self, samples: Dict[str, Any]):
        cpu_names = ['coretemp', 'cpu_thermal', 'k8temp', 'k10temp', 'atk0110']

        if not self.sensors:
            # TODO: 2 ** 32?
            for index in range(0, 2 ** 32):
                try:
                    name = open(f'/sys/class/hwmon/hwmon{index}/name').read().strip()
                    if name in cpu_names:
                        self.detect_cpu_sensors(index, name)
                except FileNotFoundError:
                    break

        for sensor_path in self.sensors:
            with open(sensor_path) as sensor:
                temperature = int(sensor.read().strip())
                if temperature == 0:
                    return

            samples['cpu.temperature'][sensor_path] = temperature / 1000


class DiskSampler(Sampler):
    descriptions = [
        SampleDescription('disk.all.read', 'bytes', 'counter', False),
        SampleDescription('disk.all.written', 'bytes', 'counter', False),
    ]

    def sample(self, samples: Dict[str, Any]):
        with open('/proc/diskstats') as diskstats:
            bytes_read = 0
            bytes_written = 0
            num_ops = 0

            for line in diskstats:
                # https://www.kernel.org/doc/Documentation/ABI/testing/procfs-diskstats
                [dev_major, _, dev_name, _, num_reads_merged, num_sectors_read, _, _, num_writes_merged, num_sectors_written, *_] = line.strip().split()

                # ignore device-mapper and md
                if (dev_major == 253 or dev_major == 9):
                    continue

                # Skip partitions
                if dev_name[:2] in ['sd', 'hd', 'vd'] and dev_name[-1].isdigit():
                    continue

                # Ignore nvme partitions
                if dev_name.startswith('nvme') and 'p' in dev_name:
                    continue

                bytes_read += int(num_sectors_read) * 512
                bytes_written += int(num_sectors_written) * 512
                num_ops += int(num_reads_merged) + int(num_writes_merged)

            samples['disk.all.read'] = bytes_read
            samples['disk.all.written'] = bytes_written
            samples['disk.all.ops'] = num_ops


class CGroupSampler(Sampler):
    descriptions = [
        SampleDescription('cgroup.memory.usage', 'bytes', 'instant', True),
        SampleDescription('cgroup.memory.limit', 'bytes', 'instant', True),
        SampleDescription('cgroup.memory.sw-usage', 'bytes', 'instant', True),
        SampleDescription('cgroup.memory.sw-limit', 'bytes', 'instant', True),
        SampleDescription('cgroup.cpu.usage', 'millisec', 'counter', True),
        SampleDescription('cgroup.cpu.shares', 'count', 'instant', True),
    ]

    cgroups_v2: Optional[bool] = None
    cgroups_v2_path = '/sys/fs/cgroup/'

    def read_cgroup_keyed_stat(self, samples, path, cgroup, name, key):
        with open(path) as stat:
            for line in stat:
                if not line.startswith(key):
                    continue

                value = int(line.split()[-1])
                if sys.maxsize > value > 0:
                    samples[name][cgroup] = value / 1000

    def read_cgroup_integer_stat(self, samples, path, cgroup, name):
        # Not every stat is available, such as cpu.weight
        try:
            with open(path) as stat:
                # Some samples such as "memory.max" contains "max" when there is a no limit
                try:
                    value = int(stat.read().strip())
                except ValueError:
                    return

                if sys.maxsize > value > 0:
                    samples[name][cgroup] = value
        except FileNotFoundError:
            pass

    def sample(self, samples):
        # TODO: Cgroups v1 support
        if self.cgroups_v2 is None:
            self.cgroups_v2 = os.path.exists('/sys/fs/cgroup/cgroup.controllers')

        if self.cgroups_v2_path:
            for path, _, _ in os.walk(self.cgroups_v2_path):
                cgroup = path.replace(self.cgroups_v2_path, '')  # TODO: Pathlib?

                if not cgroup:
                    continue

                self.read_cgroup_integer_stat(samples, os.path.join(path, 'memory.current'), cgroup, 'cgroup.memory.usage')
                self.read_cgroup_integer_stat(samples, os.path.join(path, 'memory.max'), cgroup, 'cgroup.memory.limit')
                self.read_cgroup_integer_stat(samples, os.path.join(path, 'memory.swap.current'), cgroup, 'cgroup.memory.sw-usage')
                self.read_cgroup_integer_stat(samples, os.path.join(path, 'memory.swap.max'), cgroup, 'cgroup.memory.sw-limit')
                self.read_cgroup_integer_stat(samples, os.path.join(path, 'cpu.weight'), cgroup, 'cgroup.cpu.shares')
                self.read_cgroup_keyed_stat(samples, os.path.join(path, 'cpu.stat'), cgroup, 'cgroup.cpu.usage', 'usage_usec')


class NetworkSampler(Sampler):
    descriptions = [
        SampleDescription('network.interface.tx', 'bytes', 'counter', True),
        SampleDescription('network.interface.rx', 'bytes', 'counter', True),
    ]

    def sample(self, samples):
        with open("/proc/net/dev") as network_samples:
            for line in network_samples:
                fields = line.split()

                # Skip header line
                if fields[0][-1] != ':':
                    continue

                iface = fields[0][:-1]
                samples['network.interface.rx'][iface] = int(fields[1])
                samples['network.interface.tx'][iface] = int(fields[9])


class MountSampler(Sampler):
    descriptions = [
        SampleDescription('mount.total', 'bytes', 'instant', True),
        SampleDescription('mount.used', 'bytes', 'instant', True),
    ]

    def sample(self, samples):
        with open('/proc/mounts') as mounts:
            for line in mounts:
                # Only look at real devices
                if line[0] != '/':
                    continue

                path = line.split()[1]
                res = os.statvfs(path)
                if res:
                    frsize = res.f_frsize
                    total = frsize * res.f_blocks
                    samples['mount.total'][path] = total
                    samples['mount.used'][path] = total - frsize * res.f_bfree


class BlockSampler(Sampler):
    descriptions = [
        SampleDescription('block.device.read', 'bytes', 'counter', True),
        SampleDescription('block.device.written', 'bytes', 'counter', True),
    ]

    def sample(self, samples):
        with open('/proc/diskstats') as diskstats:
            for line in diskstats:
                # https://www.kernel.org/doc/Documentation/ABI/testing/procfs-diskstats
                [_, _, dev_name, _, _, sectors_read, _, _, _, sectors_written, *_] = line.strip().split()

                samples['block.device.read'][dev_name] = int(sectors_read) * 512
                samples['block.device.written'][dev_name] = int(sectors_written) * 512


SAMPLERS = [
    BlockSampler,
    CGroupSampler,
    CPUSampler,
    CPUTemperatureSampler,
    DiskSampler,
    MemorySampler,
    MountSampler,
    NetworkSampler,
]
