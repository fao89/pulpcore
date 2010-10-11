#
# Copyright (c) 2010 Red Hat, Inc.
#
# This software is licensed to you under the GNU General Public License,
# version 2 (GPLv2). There is NO WARRANTY for this software, express or
# implied, including the implied warranties of MERCHANTABILITY or FITNESS
# FOR A PARTICULAR PURPOSE. You should have received a copy of GPLv2
# along with this software; if not, see
# http://www.gnu.org/licenses/old-licenses/gpl-2.0.txt.
#
# Red Hat trademarks are not licensed under GPLv2. No permission is
# granted to use or replicate Red Hat trademarks that are incorporated
# in this software or its documentation.
#

"""
Contains repo management (backend) classes.
"""

import os
from iniparse import ConfigParser as Parser
from pulp.client import ConsumerId
from pulp.client.connection import ConsumerConnection, RepoConnection
from pulp.client.lock import Lock
from pulp.client.config import Config
from pulp.client.logutil import getLogger

log = getLogger(__name__)


class ActionLock(Lock):
    """
    Action lock.
    @cvar PATH: The lock file absolute path.
    @type PATH: str
    """

    PATH = '/var/run/subsys/pulp/repolib.pid'

    def __init__(self):
        Lock.__init__(self, self.PATH)


class RepoLib:
    """
    Library for performing yum repo management.
    @ivar lock: The action lock.  Ensures only 1 instance updating repos.
    @type lock: L{Lock}
    """

    def __init__(self, lock=ActionLock()):
        """
        @param lock: A lock.
        @type lock: L{Lock}
        """
        self.lock = lock

    def update(self):
        """
        Update yum repos based on pulp bind (subscription).
        """
        lock = self.lock
        lock.acquire()
        try:
            action = UpdateAction()
            return action.perform()
        finally:
            lock.release()


class Pulp:
    """
    The pulp server.
    """
    def __init__(self, cfg):
        host = cfg.server.host
        port = cfg.server.port
        self.rapi = RepoConnection(host=host, port=port)
        self.capi = ConsumerConnection(host=host, port=port)

    def getProducts(self):
        """
        Get subscribed products.
        @return: A list of products
        @rtype: list
        """
        repos = []
        product = dict(content=repos)
        products = (product,)
        cid = self.consumerId()
        consumer = self.capi.consumer(cid)
        for repoid in consumer['repoids']:
            repo = self.rapi.repository(repoid)
            if repo:
                repos.append(repo)
        return products
    
    def consumerId(self):
        return str(ConsumerId())


class Action:
    """
    Action base class.
    """

    def __init__(self, ):
        self.cfg = Config()
        self.pulp = Pulp(self.cfg)


class UpdateAction(Action):
    """
    Update the yum repositores based on pulp bindings (subscription).
    """

    def perform(self):
        """
        Perform the action.
          - Get the content set(s) from pulp.
          - Merge with yum .repo file.
          - Write the merged .repo file.
        @return: The number of updates performed.
        @rtype: int
        """
        repod = RepoFile()
        repod.read()
        valid = set()
        updates = 0
        for cont in self.getUniqueContent():
            valid.add(cont.id)
            existing = repod.section(cont.id)
            if existing is None:
                updates += 1
                repod.add(cont)
                continue
            updates += existing.update(cont)
            repod.update(existing)
        keylib = KeyLib()
        for section in repod.sections():
            if section not in valid:
                updates += 1
                repod.delete(section)
                keylib.clean(section, True)
        repod.write()
        return updates

    def getUniqueContent(self):
        """
        Get the B{unique} content sets from pulp.
        @return: A unique set L{Repo} objects reported by pulp.
        @rtype: [L{Repo},...]
        """
        unique = set()
        products = self.pulp.getProducts()
        baseurl = self.cfg.cds.baseurl
        for product in products:
            for r in self.getContent(product, baseurl):
                unique.add(r)
        return unique

    def getContent(self, product, baseurl):
        """
        Get L{Repo} object(s) for a given subscription.
        @param product: A product (contains content sets).
        @type product: dict
        @param baseurl: The yum base url to be joined with relative url.
        @type baseurl: str
        @return: A list of L{Repo} objects for the specified product.
        @rtype: [L{Repo},...]
        """
        lst = []
        keylib = KeyLib()
        for cont in product['content']:
            if not cont:
                continue
            id = cont['id']
            path = cont['relative_path']
            keys = keylib.update(id, cont.get('gpgkeys', []))
            repo = Repo(id)
            repo['name'] = cont['name']
            repo['baseurl'] = self.join(baseurl, path)
            repo['enabled'] = cont.get('enabled', '1')
            repo['gpgkey'] = self.fmt(keys)
            lst.append(repo)
        return lst

    def fmt(self, v):
        """
        Format the value.
        @param v: The property value to format.
        @type v: object
        @return: The formatted key, value item.
        @rtype: tuple
        """
        if isinstance(v, (list,tuple)):
            v = '\n'.join(v)
        return v

    def join(self, base, url):
        """
        Join the base and url.
        @param base: The URL base (protocol, host & port).
        @type base: str
        @param url: The relative url.
        @type url: str
        @return: The complete (joined) url.
        @rtype: str
        """
        if '://' in url:
            return url
        if base.endswith('/'):
            base = base[:-1]
        if url.startswith('/'):
            url = url[1:]
        return '/'.join((base, url))


class Repo(dict):
    """
    A yum repo (content set).
    @cvar CA: The absolute path to the CA.
    @type CA: str
    @cvar PROPERTIES: Yum repo property definitions.
    @type PROPERTIES: tuple.
    """

    CA = None

    # (name, mutable, default)
    PROPERTIES = (
        ('name', 0, None),
        ('baseurl', 0, None),
        ('enabled', 1, '1'),
        ('gpgkey', 0, None),
        ('sslverify', 0, '0'),
    )

    def __init__(self, id):
        """
        @param id: The repo (unique) id.
        @type id: str
        """
        self.id = id
        for k,m,d in self.PROPERTIES:
            self[k] = d

    def items(self):
        """
        Get I{ordered} items.
        @return: A list of ordered items.
        @rtype: list
        """
        lst = []
        for k,m,d in self.PROPERTIES:
            v = self.get(k)
            lst.append((k,v))
        return tuple(lst)

    def update(self, other):
        """
        Update (merge) based on property definitions.
        @param other: The object to merge.
        @type other: L{Repo}.
        @return: The number of properties updated.
        @rtype: int
        """
        count = 0
        for k,m,d in self.PROPERTIES:
            v = other.get(k)
            if m:
                continue
            if self.__eq(self[k], v):
                continue
            self[k] = v
            count += 1
        return count

    def __eq(self, a, b):
        if a and b:
            return ( a == b )
        if (not a) and (not b):
            return True
        return False

    def __str__(self):
        s = []
        s.append('[%s]' % self.id)
        for k,v in self.items():
            s.append('%s = %s' % (k,v))
        return '\n'.join(s)

    def __repr__(self):
        return str(self)

    def __eq__(self, other):
        return ( self.id == other.id )

    def __hash__(self):
        return hash(self.id)


class RepoFile(Parser):
    """
    Represents a .repo file and is primarily a wrapper around
    I{iniparse} to get around its short comings and understand L{Repo} objects.
    @cvar PATH: The absolute path to a .repo file.
    @type PATH: str
    """

    PATH = '/etc/yum.repos.d/'

    def __init__(self, name='pulp.repo'):
        """
        @param name: The .repo file name.
        @type name: str
        """
        Parser.__init__(self)
        self.path = os.path.join(self.PATH, name)
        self.create()

    def read(self):
        """
        Read and parse the file.
        """
        r = Reader(self.path)
        Parser.readfp(self, r)

    def write(self):
        """
        Write the file.
        """
        f = open(self.path, 'w')
        Parser.write(self, f)
        f.close()

    def add(self, repo):
        """
        Add a repo and create section if needed.
        @param repo: A repo to add.
        @type repo: L{Repo}
        """
        self.add_section(repo.id)
        self.update(repo)

    def delete(self, section):
        """
        Delete a section (repo name).
        @return: self
        @rtype: L{RepoFile}
        """
        return self.remove_section(section)

    def update(self, repo):
        """
        Update the repo section using the specified repo.
        @param repo: A repo used to update.
        @type repo: L{Repo}
        """
        for k,v in repo.items():
            if v:
                Parser.set(self, repo.id, k, v)
            else:
                self.clear(repo, k)

    def clear(self, repo, option):
        """
        Remove the specified option.
        @param repo: A repo used to update.
        @type repo: L{Repo}
        @param option: A option name.
        @type option: str
        """
        try:
            self.remove_option(repo.id, option)
        except:
            pass

    def section(self, section):
        """
        Get a L{Repo} (section) by name.
        @param section: A section (repo) name.
        @type section: str
        @return: A repo for the section name.
        @rtype: L{Repo}
        """
        if self.has_section(section):
            repo = Repo(section)
            for k,v in self.items(section):
                repo[k] = v
            return repo

    def create(self):
        """
        Create the .repo file with appropriate header/footer
        if it does not already exist.
        """
        if os.path.exists(self.path):
            return
        f = open(self.path, 'w')
        s = []
        s.append('#')
        s.append('# Pulp Repositories')
        s.append('# Managed by Pulp client')
        s.append('#')
        f.write('\n'.join(s))
        f.close()


class Reader:
    """
    Reader object used to mitigate annoying behavior of
    iniparse of leaving blank lines when removing sections.
    """

    def __init__(self, path):
        """
        @param path: The absolute path to a .repo file.
        @type path: str
        """
        f = open(path)
        bfr = f.read()
        self.idx = 0
        self.lines = bfr.split('\n')
        f.close()

    def readline(self):
        """
        Read the next line.
        Strips annoying blank lines left by iniparse when
        removing sections.
        @return: The next line (or None).
        @rtype: str
        """
        nl = 0
        i = self.idx
        eof = len(self.lines)
        while 1:
            if i == eof:
                return
            ln = self.lines[i]
            i += 1
            if not ln:
                nl += 1
            else:
                break
        if nl:
            i -= 1
            ln = '\n'
        self.idx = i
        return ln


class KeyLib:
    """
    Provides GPG key management.
    @cvar ROOT: The root GPG keys directory.
    @type ROOT: str
    """

    ROOT = '/etc/pki/rpm-gpg/pulp'

    @classmethod
    def path(cls, repoid):
        return os.path.join(cls.ROOT, repoid)

    def update(self, repoid, keys):
        """
        Update the GPG keys.
        @param repoid: A repo id.
        @type repoid: str
        @param keys: A list of GPG keys.
        @type keys: [str,..]
        @return: A list of URLs to local files containing
            the GPG keys for the repo.
        @rtype: list
        """
        n = 0
        files = []
        self.mkdir(repoid)
        self.clean(repoid)
        for key in keys:
            if not n:
                fn = 'primary'
            else:
                fn = 'alt-%d' % n
            path = os.path.join(self.path(repoid), fn)
            f = open(path, 'w')
            f.write(key)
            f.close()
            files.append('file://%s' % path)
            n += 1
        return files

    def clean(self, repoid, rmdir=False):
        """
        Remove all GPG keys for the specified repoid.
        @param repoid: A repo id.
        @type repoid: str
        @param rmdir: Flag to remove the directory.
        @type rmdir: bool
        """
        d = self.path(repoid)
        if not os.path.exists(d):
            return
        for fn in os.listdir(d):
            path = os.path.join(d, fn)
            os.unlink(path)
        if rmdir:
            os.rmdir(d)

    def mkdir(self, repoid):
        """
        Create a directory for a repo's GPG keys.
        @param repoid: A repo id.
        @type repoid: str
        """
        path = self.path(repoid)
        if not os.path.exists(path):
            os.makedirs(path)


def main():
    print 'Updating Pulp repository'
    repolib = RepoLib()
    updates = repolib.update()
    print '%d updates required' % updates
    print 'done'

if __name__ == '__main__':
    main()
