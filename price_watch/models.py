# -*- coding: utf-8 -*-

import os
import datetime
import yaml
import json
import numpy
import urllib
import re

from uuid import uuid4
from ZODB import DB
from ZODB.FileStorage import FileStorage
from ZODB.MappingStorage import MappingStorage
from persistent import Persistent
from operator import attrgetter
from BTrees import OOBTree


HOUR_AGO = datetime.datetime.now() - datetime.timedelta(hours=1)
DAY_AGO = datetime.datetime.now() - datetime.timedelta(days=1)
WEEK_AGO = datetime.datetime.now() - datetime.timedelta(weeks=1)
TWO_WEEKS_AGO = datetime.datetime.now() - datetime.timedelta(weeks=2)
MONTH_AGO = datetime.datetime.now() - datetime.timedelta(days=30)

# time, during which report's price matters
REPORT_LIFETIME = datetime.timedelta(weeks=1)


def get_delta(base_price, current_price, relative=True):
    """Return delta relative or absolute"""
    try:
        abs_delta = current_price - base_price
        if relative:
            return abs_delta / base_price
        else:
            return abs_delta
    except TypeError:
        return 0


def load_data_map(node):
    """Return parsed `data_map.yaml`"""

    dir_ = os.path.dirname(__file__)
    filename = os.path.join(dir_, 'data_map.yaml')
    with open(filename) as map_file:
        return yaml.safe_load(map_file)[node]


def mixed_keys(list_):
    """Return combined list of str items and dict first keys (for parsed yaml
       structures)"""
    result = list()
    for item in list_:
        if type(item) is str:
            result.append(item)
        else:
            result.append(item.keys()[0])
    return result


def traverse(target, category_node, return_parent=False):
    """
    Recursively look for appropriate category from the tree in
    `data_map.yaml`
    """
    try:
        subcategories = category_node['sub']
        if target in subcategories:
            if return_parent:
                return category_node
            else:
                return subcategories[target]
        for key in subcategories:
            match = traverse(target, subcategories[key],
                             return_parent=return_parent)
            if match:
                return match
    except (KeyError, TypeError):
        pass
    return None


def keyword_lookup(string_, data_map):
    """
    Recursively look for appropriate category from the tree in
    `data_map.yaml`, checking by presence of a category keyword in the string
    """
    requirements_met = list()
    if 'keyword' not in data_map:
        requirements_met.append(False)
    else:
        keyphrases_in_string = list()
        key_phrases = data_map['keyword'].split(', ')
        for phrase in key_phrases:
            phrase_in_string = list()
            phrase_parts = phrase.split(' ')
            for phrase_part in phrase_parts:
                phrase_in_string.append(phrase_part in string_)
            keyphrases_in_string.append(all(phrase_in_string))
        requirements_met.append(any(keyphrases_in_string))
    if 'stopword' in data_map:
        stopword_phrases = data_map['stopword'].split(', ')
        stopphrase_not_in_string = list()
        for phrase in stopword_phrases:
            part_not_in_string = list()
            phrase_parts = phrase.split(' ')
            for part in phrase_parts:
                part_not_in_string.append(part not in string_)
            stopphrase_not_in_string.append(all(part_not_in_string))
        requirements_met.append(all(stopphrase_not_in_string))
    if all(requirements_met):
        return data_map
    if 'sub' in data_map:
        subcategories = data_map['sub']
        for key in subcategories:
            category_data = subcategories[key]
            if category_data:
                match = keyword_lookup(string_, category_data)
                if match:
                    return match


class PackageLookupError(Exception):
    """Exception for package not found in `data_map.yaml`"""
    def __init__(self, product):
        message = u'Package lookup failed ' \
                  u'for product "{0}"'.format(product.title)

        Exception.__init__(self, message)
        self.product = product


class CategoryLookupError(Exception):
    """Exception for category not found in `data_map.yaml`"""
    def __init__(self, product):
        message = u'Category lookup failed for ' \
                  u'product "{}"'.format(product.title)
        Exception.__init__(self, message)
        self.product = product


class StorageManager(object):
    """Persistence tool for entity instances."""

    __name__ = None

    def __init__(self, path=None, zodb_storage=None, connection=None):
        if all([path, zodb_storage, connection]) is False:
            zodb_storage = MappingStorage('test')
        if path is not None:
            zodb_storage = FileStorage(path)
        if zodb_storage is not None:
            self._db = DB(zodb_storage)
            self._zodb_storage = zodb_storage
            self.connection = self._db.open()
        if connection is not None:
            self.connection = connection
            self._db = self.connection._db
        self._root = self.connection.root()

    def __getitem__(self, namespace):
        """Container behavior"""
        return self._root[namespace]

    def __resource_url__(self, request, info):
        """For compatibility with pyramid traversal"""
        return info['app_url']

    def register(self, *instances):
        """Register new instances to appropriate namespaces"""
        for instance in instances:
            namespace = instance.namespace
            if namespace not in self._root:
                self._root[namespace] = OOBTree.BTree()
            if instance.key not in self._root[namespace]:
                self._root[namespace][instance.key] = instance

    def delete(self, *instances):
        """Delete instances from appropriate namespaces"""
        for instance in instances:
            instance.delete_from(self)

    def delete_key(self, namespace, key):
        """Delete given key in the namespace"""
        try:
            del self._root[namespace][key]
            return True
        except KeyError:
            return False

    def get(self, namespace, key):
        """Get instance from appropriate namespace by the key"""
        try:
            return self._root[namespace][key]
        except KeyError:
            return None

    def get_all(self, namespace, objects_only=True):
        """Get all instances from namespace"""
        result = None
        if namespace in self._root:
            result = self._root[namespace]
        if objects_only:
            return result.values()
        else:
            return result

    def close(self):
        """Close ZODB connection and storage"""
        self.connection.close()
        self._zodb_storage.close()

    def load_fixtures(self, path):
        """Load fixtures from JSON file in path. Mostly for testing"""
        result = dict()
        with open(path) as f:
            fixture_list = json.load(f)
            for fixture in fixture_list:
                entity_class_name = fixture.pop('class')
                import sys
                entity_class = getattr(sys.modules[__name__],
                                       entity_class_name)
                instance, stats = entity_class.assemble(
                    storage_manager=self, **fixture)
                if entity_class.namespace not in result:
                    result[entity_class.namespace] = list()
                result[entity_class.namespace].append(instance)
        return result

    def pack(self):
        """Perform ZODB pack"""
        self._db.pack()


class Entity(Persistent):
    """Master class to inherit from"""
    _representation = u'{title}'
    _key_pattern = u'{title}'
    namespace = None

    def __eq__(self, other):
        return self.key == other.key

    def __str__(self):
        return str(self.__repr__().encode('utf-8'))

    def __unicode__(self):
        return self.__repr__()

    def __repr__(self):
        """Unique representational string"""
        return self._representation.format(**self.__dict__)

    @property
    def __name__(self):
        """For compatibility with pyramid traversal"""
        return self.key

    def _get_inner_container(self):
        """
        Return the inner container if the instance has it
        """
        if hasattr(self, '_container_attr'):
            container_name = getattr(self, '_container_attr')
            container = getattr(self, container_name)
            return container
        else:
            raise NotImplementedError

    def add(self, *instances):
        """Add child instances to the instance container"""
        container = self._get_inner_container()
        for instance in instances:
            if instance not in container:
                container.append(instance)
                self._p_changed = True

    def remove(self, *instances):
        """
        Remove child instances from the instance container
        """
        container = self._get_inner_container()
        for instance in instances:
            if instance in container:
                container.remove(instance)
                self._p_changed = True

    def __contains__(self, key):
        """Container behaviour"""
        inner_container = self._get_inner_container()
        return key in inner_container

    def __resource_url__(self, request, info):
        """For compatibility with pyramid traversal"""
        parts = {
            'app_url': info['app_url'],
            'collection': self.namespace,
            'key': urllib.quote(self.key.encode('utf-8'), safe='')
        }
        return u'{app_url}/{collection}/{key}'.format(**parts)

    @property
    def key(self):
        """Return unique key based on `_key_pattern`"""
        raw_key = self._key_pattern.format(**self.__dict__)
        return raw_key.replace('/', '-')

    @classmethod
    def fetch(cls, key, storage_manager):
        """Fetch instance from storage"""
        return storage_manager.get(cls.namespace, key)

    @classmethod
    def fetch_all(cls, storage_manager, objects_only=True):
        """Fetch all instances from storage"""
        return storage_manager.get_all(cls.namespace, objects_only)

    @classmethod
    def acquire(cls, key_attr, storage_manager, return_tuple=False):
        """
        Fetch or register new instance and return it in tuple
        with status
        """
        created_new = False
        key = cls(key_attr).key
        stored_instance = cls.fetch(key, storage_manager)

        if not stored_instance:
            new_instance = cls(key_attr)
            storage_manager.register(new_instance)
            stored_instance = new_instance
            created_new = True
        if return_tuple:
            return stored_instance, created_new
        else:
            return stored_instance

    @classmethod
    def assemble(cls, **kwargs):
        """
        Minimal data instance factory. Encouraged to be the only way of
        initializing entities.
        """
        raise NotImplementedError

    def delete_from(self, storage_manager):
        """Properly delete class instance from the storage_manager"""
        raise NotImplementedError


class PriceReport(Entity):
    """Price report model, the working horse"""
    _representation = u'{price_value}-{product}-{merchant}-{reporter}'
    _key_pattern = '{uuid}'
    namespace = 'reports'

    def __init__(self, price_value, product, reporter, merchant,
                 url=None, date_time=None, uuid=None, sku=None):
        self.uuid = uuid or uuid4()
        # The Stock Keeping Unit (SKU) http://schema.org/sku
        if sku:
            self.sku = sku
        self.date_time = date_time or datetime.datetime.now()
        self.merchant = merchant
        self.product = product
        self.price_value = price_value
        self.normalized_price_value = self._get_normalized_price(price_value)
        self.reporter = reporter
        self.url = url

    def _get_normalized_price(self, price_value):
        """Return `normal` package price value for a product"""

        package = self.product.get_package()
        ratio = package.get_ratio(self.product.category)

        return price_value / ratio

    def delete_from(self, storage_manager):
        """Delete the report from product and storage"""
        try:
            self.product.reports.remove(self)
        except (KeyError, AttributeError):
            pass
        storage_manager.delete_key(self.namespace, self.key)

    @classmethod
    def acquire(cls, key, storage_manager, return_tuple=False):
        """Disallow acquiring of reports. Only pure fetching!"""

        raise NotImplementedError

    @classmethod
    def assemble(cls, storage_manager, price_value, product_title,
                 merchant_title, reporter_name, url, date_time=None,
                 uuid=None, sku=None):
        """
        The only encouraged factory method for price reports and all the
        referenced instances:
          - product
          - category
          - package
          - merchant
          - reporter
        `date_time` is expected to be str in `%Y-%m-%d %H:%M:%S` format or
        datetime object.
        New report is registered in storage.
        """

        if type(date_time) in (str, unicode):
            try:
                date_time = datetime.datetime.strptime(date_time,
                                                       '%Y-%m-%d %H:%M:%S')
            except ValueError:
                #  microseconds in string?
                date_time = datetime.datetime.strptime(date_time.split('.')[0],
                                                       '%Y-%m-%d %H:%M:%S')

        prod_is_new = cat_is_new = pack_is_new = False
        product_key = Product(product_title).key
        product = Product.fetch(product_key, storage_manager)
        if not product:
            prod_is_new = True
            product, stats = Product.assemble(storage_manager, product_title)

        # merchant
        merchant_key = Merchant(merchant_title).key
        merchant = Merchant.acquire(merchant_key, storage_manager)
        product.add_merchant(merchant)
        merchant.add_product(product)

        # report
        reporter = Reporter.acquire(reporter_name, storage_manager)
        report = cls(price_value=float(price_value), product=product,
                     reporter=reporter, merchant=merchant, url=url,
                     date_time=date_time, uuid=uuid, sku=sku)
        product.add_report(report)

        storage_manager.register(report)

        stats = prod_is_new, cat_is_new, pack_is_new

        return report, stats


class Merchant(Entity):
    """Merchant model"""
    _representation = u'{title}-{location}'
    namespace = 'merchants'
    _container_attr = 'products'

    def __init__(self, title, location=None):
        self.title = title
        self.location = location
        self.products = list()

    def patch(self, data, storage_manager):
        """Update merchant from dict. Return `True` if new key created"""
        old_key = self.key
        if 'title' in data:
            self.title = data['title']
        if 'location' in data:
            self.location = data['location']
        if old_key != self.key:
            storage_manager.register(self)
            try:
                storage_manager.delete_key(self.namespace, old_key)
            except KeyError:
                pass

    def add_product(self, product):
        """Add product to products list"""
        self.add(product)

    def remove_product(self, product):
        """Remove product from list"""
        self.remove(product)

    @classmethod
    def assemble(cls, storage_manager, title, location=None):
        """The merchant instance factory"""
        merchant = cls.acquire(title, storage_manager)
        merchant.location = location
        storage_manager.register(merchant)
        return merchant, None


class ProductPackage(Entity):
    """Product package model"""
    namespace = 'packages'

    def __init__(self, title):
        self.title = title
        self.categories = OOBTree.BTree()

    def get_data(self, attribute, default=None):
        """Get category data from `data_map.yaml`"""
        data_map = load_data_map(self.__class__.__name__)
        try:
            data = data_map[self.title][attribute]
            return data
        except KeyError:
            return default

    def is_normal(self, category):
        """Check if the package is `normal` for a product's category"""
        cat_canonical = category.get_data('normal_package')
        return cat_canonical == self.title

    def convert(self, to_unit, category):
        """Convert instance amount to amount in given units"""
        pack_amount, from_unit = self.title.split(' ')
        density = float(category.get_data('density'))
        pack_amount = float(pack_amount)
        if from_unit == 'kg' and to_unit == 'l':
            m3 = pack_amount / density  # pack_amount is weight
            liters = m3 * 1000
            return liters
        if from_unit == 'l' and to_unit == 'kg':
            kilogramms = density * pack_amount  # pack_amount is volume
            return kilogramms

    def get_ratio(self, category):
        """Get ratio to the normal package"""
        norm_package = category.get_data('normal_package')
        norm_amount, norm_unit = norm_package.split(' ')
        pack_amount, pack_unit = self.title.split(' ')
        if norm_unit != pack_unit:
            pack_amount = self.convert(norm_unit, category)
        result = float(pack_amount) / float(norm_amount)
        return result


class Category(Entity):
    """
    General category (type). It can contain other categories but not
    products
    """
    _container_attr = 'categories'
    namespace = 'types'

    def __init__(self, title):
        self.title = title
        self.categories = list()

    def add_categories(self, *categories):
        """Add child categories to the category"""
        self.add(*categories)

    def get_data(self, attribute):
        """Get category data from `data_map.yaml`"""
        data_map = load_data_map(ProductCategory.__name__)
        category = traverse(self.title, data_map)
        try:
            data = category[attribute]
            return data
        except KeyError:
            return None


class ProductCategory(Entity):
    """
    Product category model. It can contain only products,
    not other categories
    """
    _container_attr = 'products'
    namespace = 'categories'

    def __init__(self, title, category=None):
        self.title = title
        self.products = list()
        self.category = category

    def get_data(self, attribute, default=None):
        """Get category data from `data_map.yaml`"""
        data_map = load_data_map(self.__class__.__name__)
        category = traverse(self.title, data_map)
        try:
            data = category[attribute]
            return data
        except KeyError:
            return default

    def get_category_key(self):
        """
        Get parent category key using `traverse`. Only `self.title` required
        """
        # TODO decide if this should be taken from storage by default
        data_map = load_data_map(self.__class__.__name__)
        parent_category_dict = traverse(self.title, data_map,
                                        return_parent=True)
        try:
            return parent_category_dict['title']
        except TypeError:
            return None

    def add_product(self, *products):
        """Add product(s) to the category and set category to the products"""

        for product in products:
            product.category = self
            self.add(product)

    def remove_product(self, product):
        """
        Remove product from the category and set its `category`
        attribute to None
        """
        product.category = None
        self.remove(product)

    def get_reports(self, date_time=None):
        """Get price reports for the category by datetime"""

        result = list()
        for product in self.products:
            result.extend(product.get_reports(date_time))
        return result

    def get_qualified_products(self, date_time=None, location=None):
        """
        Return product and price list to datetime and location filtered by
        qualification conditions
        """

        min_package_ratio = self.get_data('min_package_ratio')
        products = self.products
        filtered_products = list()
        for product in products:

            package_fit = True
            if min_package_ratio:
                package_fit = product.package_ratio >= float(min_package_ratio)

            # Actual qualification
            product_price = product.get_price(date_time, location=location)
            if package_fit and product_price:
                filtered_products.append((product, product_price))

        return filtered_products

    def get_prices(self, date_time=None, location=None):
        """
        Fetch last known to `date_time` prices filtering by `min_package_ratio`
        and location
        """
        product_tuples = self.get_qualified_products(date_time, location)
        return [t[1] for t in product_tuples]

    def get_price(self, date_time=None, prices=None, cheap=False,
                  location=None):
        """
        Get median or minimum price for the date and optionally location
        """

        prices = prices or self.get_prices(date_time, location)
        if len(prices):
            if cheap:
                try:
                    return min(prices)
                except ValueError:
                    return None
            else:
                prices = numpy.array(prices)
                return round(numpy.median(prices), 2)
        return None

    def get_price_delta(self, date_time, relative=True, location=None):

        base_price = self.get_price(date_time, location=location)
        current_price = self.get_price(location=location)
        return get_delta(base_price, current_price, relative)

    def get_locations(self):
        """
        Get category's merchant locations. Load merchants from root if
        provided. The root trick is needed for threaded cache as lazy-loading
        of `product.merchants.values()` needs new connection.
        """
        locations = list()
        merchants = list()
        for product in self.products:
            for merchant in product.merchants:
                merchants.append(merchant)
        for merchant in merchants:
            if (merchant.location is not None) and \
                    (merchant.location not in locations):
                locations.append(merchant.location)
        return locations


class Product(Entity):
    """Product model"""

    _container_attr = 'reports'
    namespace = 'products'

    def __init__(self, title, category=None, manufacturer=None, package=None,
                 package_ratio=None):
        self.title = title
        self.manufacturer = manufacturer
        self.category = category
        if self.category is not None:
            self.category.add_product(self)
        self.package = package
        self.package_ratio = package_ratio
        self.reports = list()
        self.merchants = list()

    @classmethod
    def assemble(cls, storage_manager, title, sku=None):
        """The product instance factory"""
        product = cls(title=title)

        # early get critical info or raise exceptions
        product_category_key = product.get_category_key()
        package_key = product.get_package_key()

        # product category
        product_category, cat_is_new = ProductCategory.acquire(
            product_category_key, storage_manager, True)
        product_category.add_product(product)
        product.category = product_category

        # package
        package, pack_is_new = ProductPackage.acquire(package_key,
                                                      storage_manager,
                                                      True)
        product.package = package
        product.package_ratio = package.get_ratio(product_category)

        # category
        category_key = product_category.get_category_key()
        category = Category.acquire(category_key, storage_manager)
        category.add(product_category)
        product_category.category = category

        storage_manager.register(product, product_category, category)
        stats = cat_is_new, pack_is_new
        return product, stats

    def add_report(self, report):
        """Add report"""
        self.add(report)

    def add_merchant(self, merchant):
        """Add merchant if it's not in list"""
        if merchant not in self.merchants:
            self.merchants.append(merchant)
            self._p_changed = True

    def get_price(self, date_time=None, normalized=True, location=None):
        """Get price for the product"""
        date_time = date_time or datetime.datetime.now()
        known_prices = list()
        for merchant in self.merchants:
            if location and merchant.location != location:
                break
            report = self.get_last_report(date_time=date_time,
                                          merchant=merchant)
            if report and report.date_time > date_time - REPORT_LIFETIME:
                if normalized:
                    price = report.normalized_price_value
                else:

                    price = report.price_value
                known_prices.append(price)
        if len(known_prices):
            return numpy.median(known_prices)
        else:
            return None

    def get_price_delta(self, date_time, relative=True):

        base_price = self.get_last_reported_price(date_time)
        current_price = self.get_last_reported_price()
        return get_delta(base_price, current_price, relative)

    def get_reports(self, to_date_time=None, from_date_time=None):
        """Get reports to the given date/time"""

        result = list()
        for report in self.reports:
            qualifies = True
            if to_date_time and report.date_time > to_date_time:
                qualifies = False
            if from_date_time and report.date_time < from_date_time:
                qualifies = False
            if qualifies:
                result.append(report)
        return result

    def get_package_key(self):
        """Resolve product's package key from known ones"""

        package_data = load_data_map('ProductPackage')
        for pack_key in package_data:
            for synonym in package_data[pack_key]['synonyms']:
                look_behind_patterns = [
                    u'(?<!(\d(\.|,|х|x|\*|\-))|'
                    u'(\d{2})|(\+\s)|(\s\+)|(\d\+)|'
                    u'(\.|,|х|x|\*|\s)\d)',
                    u'(?<=фасов(\.))',
                    u'(?<=№\d(\.|,))',
                    u'(?<=№\d{2}(\.|,))',
                    u'(?<=№\d{3}(\.|,))',
                ]
                for pattern in look_behind_patterns:
                    pattern = pattern + re.escape(synonym)
                    match = re.search(pattern, self.title)
                    if match:
                        if 'unlike' in package_data[pack_key]:
                            for unlike in package_data[pack_key]['unlike']:
                                if unlike in self.title:
                                    match = None
                    if match:
                        return pack_key
        raise PackageLookupError(self)

    def get_package(self):
        """Compatibility method if there's no package defined"""
        if hasattr(self, 'package') and self.package:
            return self.package
        else:
            key = self.get_package_key()
            return ProductPackage(key)

    def get_category_key(self):
        """
        Get category key from the product's title by looking up keywords
        in `data_map.yaml`
        """
        data_map = load_data_map(ProductCategory.__name__)
        category_data = keyword_lookup(self.title.lower(), data_map)
        if category_data:
            return category_data['title']
        raise CategoryLookupError(self)

    def get_last_report(self, date_time=None, merchant=None):
        """Get last (to `date_time`) report of the product"""

        date_time = date_time or datetime.datetime.now()

        def qualify(report):
            qualified = True
            if report.date_time > date_time:
                qualified = False
            if merchant and report.merchant is not merchant:
                qualified = False
            return qualified

        reports = self.reports
        # TODO decide which one to do first sorting or filtering
        if len(reports) > 0:
            sorted_reports = sorted(reports,
                                    key=attrgetter('date_time'))
            filtered_reports = filter(qualify, sorted_reports)
            try:
                return filtered_reports[-1]
            except IndexError:
                return None
        else:
            return None

    def get_last_reported_price(self, date_time=None, normalized=True):
        """Get product price last known to the date"""

        last_report = self.get_last_report(date_time)
        if last_report:
            if normalized:
                return last_report.normalized_price_value
            return last_report.price_value
        return None

    def delete_from(self, storage_manager):
        """Delete the product from all referenced objects"""
        key = self.key
        try:
            self.category.products.remove(self)
            for merchant in self.merchants:
                merchant.products.remove(self)
        except AttributeError:
            pass
        for report in self.reports:
            report.delete_from(storage_manager)
        storage_manager.delete_key(self.namespace, key)


class Reporter(Entity):
    """Reporter model"""
    _representation = u'{name}'
    _key_pattern = u'{name}'
    _container_attr = 'reports'
    namespace = 'reporters'

    def __init__(self, name):
        self.name = name
        self.reports = list()


class Page(Entity):
    """Page model (static text)"""
    _representation = u'{slug}'
    _key_pattern = u'{slug}'
    namespace = 'pages'

    def __init__(self, slug):
        self.slug = slug

    @classmethod
    def assemble(cls, storage_manager, slug):
        new_page = cls.acquire(slug, storage_manager)
        storage_manager.register(new_page)
        return new_page, None

    def delete_from(self, storage_manager):
        """Delete the page instance from storage"""
        storage_manager.delete_key(self.namespace, self.key)