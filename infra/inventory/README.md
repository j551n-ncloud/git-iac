Inventory structure
===================

Files are named `##-name` and read in order:

* `01-` - `98-`: hosts, each listed exactly once, in its functional
  group, with its address. Group names are hierarchical:
  `<area>_<role>`, collected by `<area>:children`.
* `99-organisational_groups`: only groups made of other groups
  (`:children`). No hosts and no host settings in here.

All addresses and names here are examples (`192.168.1.x`, `pve1`,
`app1`). Replace them with yours.

Where a setting goes:

* Short facts of one host (`ansible_host`, `proxmox_node`,
  `proxmox_vmid`) go on its line in `01-` - `98-`, so every host reads as
  one line.
* Anything a whole group shares goes in `group_vars/<group>.yml`
  (`edge_vpn.yml`: tailscale settings and `patch_services`).
* LXC sizes for `playbooks/provision.yml` work like classes: defaults in
  `group_vars/all.yml` (`lxc_*`), per functional group in its group_vars
  (`edge.yml`: 1 core, 512 MB), and only deviations on the host line
  (`lxc_memory=6144 lxc_disk_gb=40`). `provision.yml --check` lists every
  LXC that differs from the inventory.
* `host_vars/<host>.yml` only where one host really differs (lists,
  long notes). `host_vars/HOSTNAME.yml.example` shows the keys.
* Secrets: per-value `!vault` files next to the group they belong to
  (`group_vars/ci_runner/vault_gitlab_runner.yml`), one file per secret,
  shipped here only as `.yml.example`. The real ones are gitignored.

A host that is off or set aside stays in its group, commented out, with
the reason and date. Ansible then never touches it, and uncommenting
brings it back.

Every group has to be used by a playbook, a role or a vars file;
remove groups that stop having an effect.

`05-proxmox` lists the hypervisors (`proxmox_nodes`, root); playbooks that
target guests exclude them (`all:!proxmox_nodes`).

The deploy repo reads this inventory and adds its stack vars on top
(`managed`, `production` and `staging` are the groups it relies on).
