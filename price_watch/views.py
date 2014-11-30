# -*- coding: utf-8 -*-

import datetime
import json
import transaction
from itertools import tee, islice, chain, izip
from babel.core import Locale
from babel.numbers import format_currency
from pyramid.view import view_config, view_defaults
from pyramid.httpexceptions import (HTTPMethodNotAllowed, HTTPAccepted,
                                    HTTPBadRequest)
from dogpile.cache import make_region
from price_watch.models import (Page, PriceReport, PackageLookupError,
                                CategoryLookupError, ProductCategory, Product,
                                Reporter, Merchant)

MULTIPLIER = 1
category_region = make_region().configure(
    'dogpile.cache.memory'
)


def namespace_predicate(class_):
    """Custom predicate to check context for being a namespace"""
    def check_namespace(context, request):
        return context is request.root[class_.namespace] or \
               type(context) == class_
    return check_namespace


def previous_and_next(some_iterable):
    """
    Previous and next values inside a loop
    credit: http://stackoverflow.com/a/1012089/216042
    """
    prevs, items, nexts = tee(some_iterable, 3)
    prevs = chain([None], prevs)
    nexts = chain(islice(nexts, 1, None), [None])
    return izip(prevs, items, nexts)


def get_datetimes(days):
    """Return list with days back range"""

    result = list()
    for count in range(0, int(days)):
        date = datetime.date.today() + datetime.timedelta(-1*MULTIPLIER*count)
        date_time = datetime.datetime.combine(date,
                                              datetime.datetime.now().time())
        result.append(date_time)
    return reversed(result)


class EntityView(object):
    """View class for Milk Price Report entities"""
    def __init__(self, request):
        self.request = request
        self.context = request.context
        self.root = request.root
        self.locale = Locale(request.locale_name)
        self.delta_period = (datetime.datetime.now() -
                             datetime.timedelta(days=30))

    def currency(self, value, symbol=''):
        """Format currency value with Babel"""
        return format_currency(value, symbol, locale=self.locale)


@view_defaults(context=Page)
class PageView(EntityView):
    # @view_config(request_method='')
    pass


@view_defaults(context=Product)
class ProductView(EntityView):
    @view_config(renderer='templates/product.mako', request_method='GET')
    def get(self):
        return {}


@view_defaults(custom_predicates=(namespace_predicate(PriceReport),))
class PriceReportView(EntityView):

    @view_config(request_method='GET', renderer='templates/report.mako')
    def get(self):
        return {}

    @view_config(request_method='POST', renderer='json')
    def post(self):
        # TODO Implement validation
        post_data = self.request.POST
        date_time = None

        if 'date_time' in post_data:
                date_time = datetime.datetime.strptime(post_data['date_time'],
                                                       '%Y-%m-%d %H:%M:%S')

        try:
            reporter = Reporter.acquire(post_data['reporter'], self.root)
            merchant = Merchant.acquire(post_data['merchant'], self.root)

            # TODO accept string data instead of objects
            report, stats_ = PriceReport.assemble(
                price_value=float(post_data['price_value']),
                product_title=post_data['product_title'],
                url=post_data['url'],
                merchant=merchant,
                reporter=reporter,
                date_time=date_time,
                storage_manager=self.root
            )
            transaction.commit()
            return {
                'new_report': report.key,
                'stats': stats_
            }
        except (KeyError, PackageLookupError, CategoryLookupError), e:
            transaction.abort()
            raise HTTPBadRequest(e.message)

    @view_config(request_method='DELETE', renderer='json')
    def delete(self):

        self.context.delete_from(self.root)
        transaction.commit()
        return {'deleted_report_key': self.context.key}


@view_defaults(context=ProductCategory)
class CategoryView(EntityView):

    @category_region.cache_on_arguments()
    def cached_data(self, category):
        """Return cached category data"""
        price_data = list()
        datetimes = get_datetimes(30)
        for date in datetimes:
            price_data.append([date.strftime('%d.%m'),
                               category.get_price(date)])
        products = list()
        sorted_products = sorted(category.get_qualified_products(),
                                 key=lambda pr: pr.get_price())
        for num, product in enumerate(sorted_products):
            price = product.get_price()
            middle_num = int(len(sorted_products) / 2)
            median = (num == middle_num)
            if len(sorted_products) % 2 == 0:
                median = (num == middle_num or num == middle_num-1)

            # construct data row as tuple
            products.append((
                num+1,
                product,
                self.request.resource_url(product),
                self.currency(price),
                int(product.get_price_delta(self.delta_period)*100),
                median
            ))
        return {'price_data': json.dumps(price_data),
                'products': products,
                'cat_title': category.get_data('keyword'),
                'median_price': self.currency(category.get_price(), u'р.')}

    @view_config(request_method='GET',
                 renderer='templates/product_category.mako')
    def get(self):
        category = self.request.context
        return self.cached_data(category)


class RootView(EntityView):
    """General root views"""

    @view_config(request_method='GET', renderer='templates/index.mako')
    def get(self):
        if self.context is self.root[ProductCategory.namespace]:
            raise HTTPMethodNotAllowed
        if self.context is self.root[PriceReport.namespace]:
            raise HTTPMethodNotAllowed(allow=['POST'])
        if self.context is self.root[Product.namespace]:
            raise HTTPMethodNotAllowed
        return {}

    @view_config(request_method='GET', name='refresh')
    def refresh(self):
        """Temporary cache cleaning. Breaks RESTfulness"""
        if self.context is self.root[ProductCategory.namespace]:
            category_region.invalidate()
            raise HTTPAccepted

        raise HTTPMethodNotAllowed