import json
import logging
import time
import os
from textwrap import dedent
from tasks.cephfs.cephfs_test_case import CephFSTestCase
from teuthology.exceptions import CommandFailedError
import teuthology.misc as misc

log = logging.getLogger(__name__)


class TestVolumeClient(CephFSTestCase):
    # One for looking at the global filesystem, one for being
    # the VolumeClient, one for mounting the created shares
    CLIENTS_REQUIRED = 3

    def _volume_client_python(self, client, script):
        # Can't dedent this *and* the script we pass in, because they might have different
        # levels of indentation to begin with, so leave this string zero-indented
        return client.run_python("""
from ceph_volume_client import CephFSVolumeClient, VolumePath
import logging
log = logging.getLogger("ceph_volume_client")
log.addHandler(logging.StreamHandler())
log.setLevel(logging.DEBUG)
vc = CephFSVolumeClient("manila", "{conf_path}", "ceph")
vc.connect()
{payload}
vc.disconnect()
        """.format(payload=script, conf_path=client.config_path))

    def _sudo_write_file(self, remote, path, data):
        """
        Write data to a remote file as super user

        :param remote: Remote site.
        :param path: Path on the remote being written to.
        :param data: Data to be written.

        Both perms and owner are passed directly to chmod.
        """
        remote.run(
            args=[
                'sudo',
                'python',
                '-c',
                'import shutil, sys; shutil.copyfileobj(sys.stdin, file(sys.argv[1], "wb"))',
                path,
            ],
            stdin=data,
        )

    def _configure_vc_auth(self, mount, id_name):
        """
        Set up auth credentials for the VolumeClient user
        """
        out = self.fs.mon_manager.raw_cluster_cmd(
            "auth", "get-or-create", "client.{name}".format(name=id_name),
            "mds", "allow *",
            "osd", "allow rw",
            "mon", "allow *"
        )
        mount.client_id = id_name
        self._sudo_write_file(mount.client_remote, mount.get_keyring_path(), out)
        self.set_conf("client.{name}".format(name=id_name), "keyring", mount.get_keyring_path())

    def test_lifecycle(self):
        """
        General smoke test for create, extend, destroy
        """

        # I'm going to use mount_c later as a guest for mounting the created
        # shares
        self.mounts[2].umount()

        # I'm going to leave mount_b unmounted and just use it as a handle for
        # driving volumeclient.  It's a little hacky but we don't have a more
        # general concept for librados/libcephfs clients as opposed to full
        # blown mounting clients.
        self.mount_b.umount_wait()
        self._configure_vc_auth(self.mount_b, "manila")

        guest_entity = "guest"
        group_id = "grpid"
        volume_id = "volid"

        # Create
        mount_path = self._volume_client_python(self.mount_b, dedent("""
            vp = VolumePath("{group_id}", "{volume_id}")
            create_result = vc.create_volume(vp, 10)
            print create_result['mount_path']
        """.format(
            group_id=group_id,
            volume_id=volume_id,
            guest_entity=guest_entity
        )))

        # Authorize
        key = self._volume_client_python(self.mount_b, dedent("""
            vp = VolumePath("{group_id}", "{volume_id}")
            auth_result = vc.authorize(vp, "{guest_entity}")
            print auth_result['auth_key']
        """.format(
            group_id=group_id,
            volume_id=volume_id,
            guest_entity=guest_entity
        )))

        # The dir should be created
        self.mount_a.stat(os.path.join("volumes", group_id, volume_id))

        # The auth identity should exist
        existing_ids = [a['entity'] for a in self.auth_list()]
        self.assertIn("client.{0}".format(guest_entity), existing_ids)

        keyring_txt = dedent("""
        [client.{guest_entity}]
            key = {key}

        """.format(
            guest_entity=guest_entity,
            key=key
        ))

        # We should be able to mount the volume
        self.mounts[2].client_id = guest_entity
        self._sudo_write_file(self.mounts[2].client_remote, self.mounts[2].get_keyring_path(), keyring_txt)
        self.set_conf("client.{0}".format(guest_entity), "debug client", "20")
        self.set_conf("client.{0}".format(guest_entity), "debug objecter", "20")
        self.set_conf("client.{0}".format(guest_entity), "keyring", self.mounts[2].get_keyring_path())
        self.mounts[2].mount(mount_path=mount_path)
        self.mounts[2].write_n_mb("data.bin", 1)

        # De-authorize the guest
        self._volume_client_python(self.mount_b, dedent("""
            vp = VolumePath("{group_id}", "{volume_id}")
            vc.deauthorize(vp, "{guest_entity}")
            vc.evict("{guest_entity}")
        """.format(
            group_id=group_id,
            volume_id=volume_id,
            guest_entity=guest_entity
        )))

        # Once deauthorized, the client should be unable to do any more metadata ops
        # The way that the client currently behaves here is to block (it acts like
        # it has lost network, because there is nothing to tell it that is messages
        # are being dropped because it's identity is gone)
        background = self.mounts[2].write_n_mb("rogue.bin", 1, wait=False)
        time.sleep(10)  # Approximate check for 'stuck' as 'still running after 10s'
        self.assertFalse(background.finished)

        # After deauthorisation, the client ID should be gone (this was the only
        # volume it was authorised for)
        self.assertNotIn("client.{0}".format(guest_entity), [e['entity'] for e in self.auth_list()])

        # Clean up the dead mount (ceph-fuse's behaviour here is a bit undefined)
        self.mounts[2].kill()
        self.mounts[2].kill_cleanup()
        try:
            background.wait()
        except CommandFailedError:
            # We killed the mount out from under you
            pass

        self._volume_client_python(self.mount_b, dedent("""
            vp = VolumePath("{group_id}", "{volume_id}")
            vc.delete_volume(vp)
            vc.purge_volume(vp)
        """.format(
            group_id=group_id,
            volume_id=volume_id,
            guest_entity=guest_entity
        )))

    def test_idempotency(self):
        """
        That the volumeclient interface works when calling everything twice
        """
        self.mount_b.umount_wait()
        self._configure_vc_auth(self.mount_b, "manila")

        guest_entity = "guest"
        group_id = "grpid"
        volume_id = "volid"
        self._volume_client_python(self.mount_b, dedent("""
            vp = VolumePath("{group_id}", "{volume_id}")
            vc.create_volume(vp, 10)
            vc.create_volume(vp, 10)
            vc.authorize(vp, "{guest_entity}")
            vc.authorize(vp, "{guest_entity}")
            vc.deauthorize(vp, "{guest_entity}")
            vc.deauthorize(vp, "{guest_entity}")
            vc.delete_volume(vp)
            vc.delete_volume(vp)
            vc.purge_volume(vp)
            vc.purge_volume(vp)

            vc.create_volume(vp, 10, data_isolated=True)
            vc.create_volume(vp, 10, data_isolated=True)
            vc.authorize(vp, "{guest_entity}")
            vc.authorize(vp, "{guest_entity}")
            vc.deauthorize(vp, "{guest_entity}")
            vc.deauthorize(vp, "{guest_entity}")
            vc.evict("{guest_entity}")
            vc.evict("{guest_entity}")
            vc.delete_volume(vp, data_isolated=True)
            vc.delete_volume(vp, data_isolated=True)
            vc.purge_volume(vp, data_isolated=True)
            vc.purge_volume(vp, data_isolated=True)
        """.format(
            group_id=group_id,
            volume_id=volume_id,
            guest_entity=guest_entity
        )))

    def test_data_isolated(self):
        """
        That data isolated shares get their own pool
        :return:
        """
        self.mount_b.umount_wait()
        self._configure_vc_auth(self.mount_b, "manila")

        # Calculate how many PGs we'll expect the new volume pool to have
        osd_map = json.loads(self.fs.mon_manager.raw_cluster_cmd('osd', 'dump', '--format=json-pretty'))
        max_per_osd = int(self.fs.get_config('mon_pg_warn_max_per_osd'))
        osd_count = len(osd_map['osds'])
        max_overall = osd_count * max_per_osd

        existing_pg_count = 0
        for p in osd_map['pools']:
            existing_pg_count += p['pg_num']

        expected_pg_num = (max_overall - existing_pg_count) / 10
        log.info("max_per_osd {0}".format(max_per_osd))
        log.info("osd_count {0}".format(osd_count))
        log.info("max_overall {0}".format(max_overall))
        log.info("existing_pg_count {0}".format(existing_pg_count))
        log.info("expected_pg_num {0}".format(expected_pg_num))

        pools_a = json.loads(self.fs.mon_manager.raw_cluster_cmd("osd", "dump", "--format=json-pretty"))['pools']

        guest_entity = "guest"
        group_id = "grpid"
        volume_id = "volid"
        self._volume_client_python(self.mount_b, dedent("""
            vp = VolumePath("{group_id}", "{volume_id}")
            vc.create_volume(vp, 10, data_isolated=True)
        """.format(
            group_id=group_id,
            volume_id=volume_id,
            guest_entity=guest_entity
        )))

        pools_b = json.loads(self.fs.mon_manager.raw_cluster_cmd("osd", "dump", "--format=json-pretty"))['pools']

        # Should have created one new pool
        new_pools = set(p['pool_name'] for p in pools_b) - set([p['pool_name'] for p in pools_a])
        self.assertEqual(len(new_pools), 1)

        # It should have followed the heuristic for PG count
        # (this is an overly strict test condition, so we may want to remove
        #  it at some point as/when the logic gets fancier)
        created_pg_num = self.fs.mon_manager.get_pool_property(list(new_pools)[0], "pg_num")
        self.assertEqual(expected_pg_num, created_pg_num)
