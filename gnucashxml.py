# gnucashxml.py --- Parse GNU Cash XML files

# Copyright (C) 2012 Jorgen Schaefer <forcer@forcix.cx>
#           (C) 2017 Christopher Lam

# Author: Jorgen Schaefer <forcer@forcix.cx>
#         Christopher Lam <https://github.com/christopherlam>

# This program is free software; you can redistribute it and/or
# modify it under the terms of the GNU General Public License
# as published by the Free Software Foundation; either version 3
# of the License, or (at your option) any later version.

# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.

# You should have received a copy of the GNU General Public License
# along with this program. If not, see <http://www.gnu.org/licenses/>.

import decimal
import gzip
from dateutil.parser import parse as parse_date

try:
    import lxml.etree as ElementTree
except:
    from xml.etree import ElementTree
from xml.etree.ElementTree import ParseError

__version__ = "1.1"

class Book(object):
    """
    A book is the main container for GNU Cash data.

    It doesn't really do anything at all by itself, except to have
    a reference to the accounts, transactions, prices, and commodities.
    """
    def __init__(self, tree, guid, prices=None, transactions=None, root_account=None,
                 accounts=None, commodities=None, slots=None):
        self.tree = tree
        self.guid = guid
        self.prices = prices
        self.transactions = transactions or []
        self.root_account = root_account
        self.accounts = accounts or []
        self.commodities = commodities or []
        self.slots = slots or {}

    def __repr__(self):
        return "<Book {}>".format(self.guid)

    def walk(self):
        return self.root_account.walk()

    def find_account(self, name):
        for account, children, splits in self.walk():
            if account.name == name:
                return account

    def find_guid(self, guid):
        for item in self.accounts + self.transactions:
            if item.guid == guid:
                return item

    def ledger(self):
        outp = []

        for comm in self.commodities:
            outp.append('commodity {}'.format(comm.name))
            outp.append('\tnamespace {}'.format(comm.space))
            outp.append('')

        for account in self.accounts:
            outp.append('account {}'.format(account.fullname()))
            if account.description:
                outp.append('\tnote {}'.format(account.description))
            outp.append('\tcheck commodity == "{}"'.format(account.commodity))
            outp.append('')

        for trn in sorted(self.transactions):
            reconciled = all(spl.reconciled_state == 'y' for spl in trn.splits)
            outp.append('{:%Y/%m/%d}{}{}{}'.format(
                trn.date,
                " *" if reconciled else "",
                " " + trn.description if trn.description is not None else "",
                " ; " + trn.slots["notes"] if 'notes' in trn.slots and trn.slots["notes"] is not None else ""))
            for spl in trn.splits:
                commodity = str(spl.account.commodity)
                if any(not c.isalpha() for c in commodity):
                    commodity = '"{}"'.format(commodity)
                price = ""
                if spl.account.commodity != trn.currency:
                    price = ' @ {:12.8f} {}'.format(abs(spl.value/spl.quantity),
                                                    trn.currency)
                outp.append('\t{}{:50}  {:12.{}f} {}{}{}'.format(
                    ("* " if not reconciled and spl.reconciled_state == 'y' else
                     "! " if not reconciled and spl.reconciled_state == 'c' else ""),
                    spl.account.fullname(),
                    spl.quantity,
                    len(str(spl.account.commodity.fraction)) - 1,
                    commodity,
                    price,
                    ' ; '+spl.memo if spl.memo else ''))
            outp.append('')

        return '\n'.join(outp)

    def ledger_price_db(self):
        outp = []

        previous_date = None
        for price in sorted(self.prices):
            if previous_date is not None and previous_date != price.date:
                outp.append('')
            previous_date = price.date
            outp.append('P {:%Y/%m/%d %H:%M:%S} {} {} {}'.format(
                price.date,
                price.commodity.name if ' ' not in price.commodity.name else '"{}"'.format(price.commodity.name),
                price.value,
                price.currency.name))
        return '\n'.join(outp)


class Commodity(object):
    """
    A commodity is something that's stored in GNU Cash accounts.

    Consists of a name (or id) and a space (namespace).
    """
    def __init__(self, name, space=None, fraction=None):
        self.name = name
        self.space = space
        self.fraction = fraction or 100

    def __str__(self):
        return self.name

    def __repr__(self):
        return "<Commodity {}:{} 1/{}>".format(self.space, self.name, self.fraction)


class Account(object):
    """
    An account is part of a tree structure of accounts and contains splits.
    """
    def __init__(self, name, guid, actype, parent=None,
                 commodity=None, commodity_scu=None,
                 description=None, slots=None):
        self.name = name
        self.guid = guid
        self.actype = actype
        self.description = description
        self.parent = parent
        self.children = []
        self.commodity = commodity
        self.commodity_scu = commodity_scu
        self.splits = []
        self.slots = slots or {}

    def fullname(self):
        if self.parent:
            pfn = self.parent.fullname()
            if pfn:
                return '{}:{}'.format(pfn, self.name)
            else:
                return self.name
        else:
            return ''

    def __repr__(self):
        return "<Account '{}[{}]' {}...>".format(self.name, self.commodity, self.guid[:10])

    def walk(self):
        """
        Generate splits in this account tree by walking the tree.

        For each account, it yields a 3-tuple (account, subaccounts, splits).

        You can modify the list of subaccounts, but should not modify
        the list of splits.
        """
        accounts = [self]
        while accounts:
            acc, accounts = accounts[0], accounts[1:]
            children = list(acc.children)
            yield (acc, children, acc.splits)
            accounts.extend(children)

    def find_account(self, name):
        for account, children, splits in self.walk():
            if account.name == name:
                return account

    def get_all_splits(self):
        split_list = []
        for account, children, splits in self.walk():
            split_list.extend(splits)
        return sorted(split_list)

    def __lt__(self,other):
        # For sorted() only
        if isinstance(other, Account):
            return self.fullname() < other.fullname()
        else:
            False


class Transaction(object):
    """
    A transaction is a balanced group of splits.
    """

    def __init__(self, guid=None, currency=None,
                 date=None, date_entered=None,
                 description=None, splits=None,
                 num=None, slots=None):
        self.guid = guid
        self.currency = currency
        self.date = date
        self.post_date = date             # for compatibility with piecash
        self.date_entered = date_entered
        self.description = description
        self.num = num or None
        self.splits = splits or []
        self.slots = slots or {}

    def __repr__(self):
        return "<Transaction on {} '{}' {}...>".format(
            self.date, self.description, self.guid[:6])

    def __lt__(self, other):
        # For sorted() only
        if isinstance(other, Transaction):
            return self.date < other.date
        else:
            False


class Split(object):
    """
    A split is one entry in a transaction.
    """

    def __init__(self, guid=None, memo=None,
                 reconciled_state=None, reconcile_date=None, value=None,
                 quantity=None, account=None, transaction=None, action=None,
                 slots=None):
        self.guid = guid
        self.reconciled_state = reconciled_state
        self.reconcile_date = reconcile_date
        self.value = value
        self.quantity = quantity
        self.account = account
        self.transaction = transaction
        self.action = action
        self.memo = memo
        self.slots = slots

    def __repr__(self):
        return "<Split {} '{}' {} {} {}...>".format(self.transaction.date,
            self.transaction.description,
            self.transaction.currency,
            self.value,
            self.guid[:6])

    def __lt__(self, other):
        # For sorted() only
        if isinstance(other, Split):
            return self.transaction < other.transaction
        else:
            False


class Price(object):
    """
    A price is GNUCASH record of the price of a commodity against a currency
    Consists of date, currency, commodity,  value
    """
    def __init__(self, guid=None, commodity=None, currency=None,
                 date=None, value=None):
        self.guid = guid
        self.commodity = commodity
        self.currency = currency
        self.date = date
        self.value = value

    def __repr__(self):
        return "<Price {}... {:%Y/%m/%d}: {} {}/{} >".format(self.guid[:6],
            self.date,
            self.value,
            self.commodity,
            self.currency)

    def __lt__(self, other):
        # For sorted() only
        if isinstance(other, Price):
            return self.date < other.date
        else:
            False


##################################################################
# XML file parsing

def from_filename(filename):
    """Parse a GNU Cash file and return a Book object."""
    try:
        # try opening with gzip decompression
        return parse(gzip.open(filename, "rb"))
    except IOError:
        # try opening without decompression
        return parse(open(filename, "rb"))


# Implemented:
# - gnc:book
#
# Not implemented:
# - gnc:count-data
#   - This seems to be primarily for integrity checks?
def parse(fobj):
    """Parse GNU Cash XML data from a file object and return a Book object."""
    try:
        tree = ElementTree.parse(fobj)
    except ParseError:
        raise ValueError("File stream was not a valid GNU Cash v2 XML file")

    root = tree.getroot()
    if root.tag != 'gnc-v2':
        raise ValueError("File stream was not a valid GNU Cash v2 XML file")
    return _book_from_tree(root.find("{http://www.gnucash.org/XML/gnc}book"))


# Implemented:
# - book:id
# - book:slots
# - gnc:commodity
# - gnc:account
# - gnc:transaction
#
# Not implemented:
# - gnc:schedxaction
# - gnc:template-transactions
# - gnc:count-data
#   - This seems to be primarily for integrity checks?
def _book_from_tree(tree):
    guid = tree.find('{http://www.gnucash.org/XML/book}id').text

    # Implemented:
    # - cmdty:id
    # - cmdty:space
    # - cmdty:fraction => optional, e.g. "1"
    #
    # Not implemented:
    # - cmdty:get_quotes => unknown, empty, optional
    # - cmdty:quote_tz => unknown, empty, optional
    # - cmdty:source => text, optional, e.g. "currency"
    # - cmdty:name => optional, e.g. "template"
    # - cmdty:xcode => optional, e.g. "template"
    def _commodity_from_tree(tree):
        name = tree.find('{http://www.gnucash.org/XML/cmdty}id').text
        space = tree.find('{http://www.gnucash.org/XML/cmdty}space').text
        fraction = tree.find('{http://www.gnucash.org/XML/cmdty}fraction')
        return Commodity(name=name, space=space, fraction=int(fraction.text) if fraction is not None else None)


    def _commodity_find(space, name):
        return commoditydict.setdefault((space,name), Commodity(name=name, space=space))

    commodities = []        # This will store the Gnucash root list of commodities
    commoditydict = {}      # This will store the list of commodities used
                            # The above two may not be equal! eg prices may include commodities
                            # that are not represented in the account tree

    for child in tree.findall('{http://www.gnucash.org/XML/gnc}commodity'):
        comm = _commodity_from_tree(child)
        commoditydict[(comm.space, comm.name)] =  comm
        commodities.append(comm)
        #COMPACT:
        #name = child.find('{http://www.gnucash.org/XML/cmdty}id').text
        #space = child.find('{http://www.gnucash.org/XML/cmdty}space').text
        #commodities.append(_commodity_find(space, name))


    # Implemented:
    # - price
    # - price:guid
    # - price:commodity
    # - price:currency
    # - price:date
    # - price:value
    def _price_from_tree(tree):
        price = '{http://www.gnucash.org/XML/price}'
        cmdty = '{http://www.gnucash.org/XML/cmdty}'
        ts = "{http://www.gnucash.org/XML/ts}"

        guid = tree.find(price + 'id').text
        value = _parse_number(tree.find(price + 'value').text)
        date = parse_date(tree.find(price + 'time/' + ts + 'date').text)

        currency_space = tree.find(price + "currency/" + cmdty + "space").text
        currency_name = tree.find(price + "currency/" + cmdty + "id").text
        currency = _commodity_find(currency_space, currency_name)

        commodity_space = tree.find(price + "commodity/" + cmdty + "space").text
        commodity_name = tree.find(price + "commodity/" + cmdty + "id").text
        commodity = _commodity_find(commodity_space, commodity_name)

        return Price(guid=guid,
                     commodity=commodity,
                     date=date,
                     value=value,
                     currency=currency)

    prices = []
    t = tree.find('{http://www.gnucash.org/XML/gnc}pricedb')
    if t is not None:
        for child in t.findall('price'):
            price = _price_from_tree(child)
            prices.append(price)

    root_account = None
    accounts = []
    accountdict = {}
    parentdict = {}

    for child in tree.findall('{http://www.gnucash.org/XML/gnc}account'):
        parent_guid, acc = _account_from_tree(child, commoditydict)
        if acc.actype == 'ROOT':
            root_account = acc
        accountdict[acc.guid] = acc
        parentdict[acc.guid] = parent_guid
    for acc in list(accountdict.values()):
        if acc.parent is None and acc.actype != 'ROOT':
            parent = accountdict[parentdict[acc.guid]]
            acc.parent = parent
            parent.children.append(acc)
            accounts.append(acc)

    transactions = []
    for child in tree.findall('{http://www.gnucash.org/XML/gnc}'
                              'transaction'):
        transactions.append(_transaction_from_tree(child,
                                                   accountdict,
                                                   commoditydict))

    slots = _slots_from_tree(
        tree.find('{http://www.gnucash.org/XML/book}slots'))
    return Book(tree=tree,
                guid=guid,
                prices=prices,
                transactions=transactions,
                root_account=root_account,
                accounts=accounts,
                commodities=commodities,
                slots=slots)





# Implemented:
# - act:name
# - act:id
# - act:type
# - act:description
# - act:commodity
# - act:commodity-scu
# - act:parent
# - act:slots
def _account_from_tree(tree, commoditydict):
    act = '{http://www.gnucash.org/XML/act}'
    cmdty = '{http://www.gnucash.org/XML/cmdty}'

    name = tree.find(act + 'name').text
    guid = tree.find(act + 'id').text
    actype = tree.find(act + 'type').text
    description = tree.find(act + "description")
    if description is not None:
        description = description.text
    slots = _slots_from_tree(tree.find(act + 'slots'))
    if actype == 'ROOT':
        parent_guid = None
        commodity = None
        commodity_scu = None
    else:
        parent_guid = tree.find(act + 'parent').text
        commodity_space = tree.find(act + 'commodity/' +
                                    cmdty + 'space').text
        commodity_name = tree.find(act + 'commodity/' +
                                   cmdty + 'id').text
        commodity_scu = tree.find(act + 'commodity-scu').text
        commodity = commoditydict[(commodity_space, commodity_name)]
    return parent_guid, Account(name=name,
                                description=description,
                                guid=guid,
                                actype=actype,
                                commodity=commodity,
                                commodity_scu=commodity_scu,
                                slots=slots)

# Implemented:
# - trn:id
# - trn:currency
# - trn:date-posted
# - trn:date-entered
# - trn:description
# - trn:splits / trn:split
# - trn:slots
def _transaction_from_tree(tree, accountdict, commoditydict):
    trn = '{http://www.gnucash.org/XML/trn}'
    cmdty = '{http://www.gnucash.org/XML/cmdty}'
    ts = '{http://www.gnucash.org/XML/ts}'
    split = '{http://www.gnucash.org/XML/split}'

    guid = tree.find(trn + "id").text
    currency_space = tree.find(trn + "currency/" +
                               cmdty + "space").text
    currency_name = tree.find(trn + "currency/" +
                               cmdty + "id").text
    currency = commoditydict[(currency_space, currency_name)]
    date = parse_date(tree.find(trn + "date-posted/" +
                                       ts + "date").text)
    date_entered = parse_date(tree.find(trn + "date-entered/" +
                                        ts + "date").text)
    description = tree.find(trn + "description").text

    #rarely used
    num = tree.find(trn + "num")
    if num is not None:
        num = num.text

    slots = _slots_from_tree(tree.find(trn + "slots"))
    transaction = Transaction(guid=guid,
                              currency=currency,
                              date=date,
                              date_entered=date_entered,
                              description=description,
                              num=num,
                              slots=slots)

    for subtree in tree.findall(trn + "splits/" + trn + "split"):
        split = _split_from_tree(subtree, accountdict, transaction)
        transaction.splits.append(split)

    return transaction


# Implemented:
# - split:id
# - split:memo
# - split:reconciled-state
# - split:reconcile-date
# - split:value
# - split:quantity
# - split:account
# - split:slots
def _split_from_tree(tree, accountdict, transaction):
    split = '{http://www.gnucash.org/XML/split}'
    ts = "{http://www.gnucash.org/XML/ts}"

    guid = tree.find(split + "id").text
    memo = tree.find(split + "memo")
    if memo is not None:
        memo = memo.text
    reconciled_state = tree.find(split + "reconciled-state").text
    reconcile_date = tree.find(split + "reconcile-date/" + ts + "date")
    if reconcile_date is not None:
        reconcile_date = parse_date(reconcile_date.text)
    value = _parse_number(tree.find(split + "value").text)
    quantity = _parse_number(tree.find(split + "quantity").text)
    account_guid = tree.find(split + "account").text
    account = accountdict[account_guid]
    slots = _slots_from_tree(tree.find(split + "slots"))
    action = tree.find(split + "action")
    if action is not None:
        action = action.text

    split = Split(guid=guid,
                  memo=memo,
                  reconciled_state=reconciled_state,
                  reconcile_date=reconcile_date,
                  value=value,
                  quantity=quantity,
                  account=account,
                  transaction=transaction,
                  action=action,
                  slots=slots)
    account.splits.append(split)
    return split


# Implemented:
# - slot
# - slot:key
# - slot:value
# - ts:date
# - gdate
def _slots_from_tree(tree):
    if tree is None:
        return {}
    slot = "{http://www.gnucash.org/XML/slot}"
    ts = "{http://www.gnucash.org/XML/ts}"
    slots = {}
    for elt in tree.findall("slot"):
        key = elt.find(slot + "key").text
        value = elt.find(slot + "value")
        type_ = value.get('type', 'string')
        if type_ in ('integer', 'double'):
            slots[key] = int(value.text)
        elif type_ == 'numeric':
            slots[key] = _parse_number(value.text)
        elif type_ in ('string', 'guid'):
            slots[key] = value.text
        elif type_ == 'gdate':
            slots[key] = parse_date(value.find("gdate").text)
        elif type_ == 'timespec':
            slots[key] = parse_date(value.find(ts + "date").text)
        elif type_ == 'frame':
            slots[key] = _slots_from_tree(value)
        else:
            raise RuntimeError("Unknown slot type {}".format(type_))
    return slots

def _parse_number(numstring):
    num, denum = numstring.split("/")
    return decimal.Decimal(num) / decimal.Decimal(denum)
