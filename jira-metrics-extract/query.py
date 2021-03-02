from past.builtins import basestring
import itertools
import datetime
import dateutil.parser
import dateutil.tz
import re
from jira import JIRA, JIRAError

def to_datetime(date):
    """Turn a date into a datetime at midnight.
    """
    return datetime.datetime.combine(date, datetime.datetime.min.time())


def strip_time(datetime):
    """Return a version of the datetime with time set to zero.
    """
    return to_datetime(datetime.date())

class IssueSnapshot(object):
    """A snapshot of the key fields of an issue at a point in its change history
    """

    def __init__(self, change, key, date, status, resolution, is_resolved):
        self.change = change
        self.key = key
        self.date = date.astimezone(dateutil.tz.tzutc())
        self.status = status
        self.resolution = resolution
        self.is_resolved = is_resolved

    def __hash__(self):
        return hash(self.key)

    def __repr__(self):
        return "<IssueSnapshot change=%s key=%s date=%s status=%s resolution=%s is_resolved=%s>" % (
            self.change, self.key, self.date.isoformat(), self.status, self.resolution, self.is_resolved
        )

class IssueSizeSnapshot(object):
    """A snapshot of the key fields of an issue at a point in its change history when its size changed
    """

    def __init__(self, change, key, date,size=None):
        self.change = change
        self.key = key
        self.date = date.astimezone(dateutil.tz.tzutc())
        self.size = size

    def __hash__(self):
        return hash(self.key)

    def __repr__(self):
        return "<IssueSnapshot change=%s key=%s date=%s size=%s>" % (
            self.change, self.key, self.date.isoformat(), self.size
        )


class QueryManager(object):
    """Manage and execute queries
    """

    settings = dict(
        queries=[],
        query_attribute=None,
        fields={},
        known_values={},
        max_results=500,
    )

    fields = {}  # resolved at runtime to JIRA fields

    def __init__(self, jira, **kwargs):
        self.jira = jira
        settings = self.settings.copy()
        settings.update(kwargs)

        self.settings = settings
        self.resolve_fields()

    # Helpers

    def resolve_fields(self):
        fields = self.jira.fields()
        for name, field in self.settings['fields'].items():
            try:
                self.fields[name] = next((f['id'] for f in fields if f['name'].lower() == field.lower()))
            except StopIteration:
                raise Exception("JIRA field with name `%s` does not exist (did you try to use the field id instead?)" % field)

    def resolve_field_value(self, issue, name, field_name):
        try:
            field_value = getattr(issue.fields, field_name)
        except AttributeError:
            field_value = None

        if field_value is None:
            return None

        value = getattr(field_value, 'value', field_value)
        try:
            child=field_value.child.value
        except:
            child = None
        if child:
            if isinstance(value, (basestring)):
                value = value + "|"+child

        if isinstance(value, (list, tuple)):
            if len(value) == 0:
                value = None
            else:
                values = [getattr(v, 'name', v) for v in value]
                if name not in self.settings['known_values']:
                    value = '|'.join([str(x) for x in values]) # values[0]
                else:
                    try:
                        value = next(itertools.ifilter(lambda v: v in values, self.settings['known_values'][name]))
                    except StopIteration:
                        value = None
        else:
            if not isinstance(value, (int, float, bool, basestring)):
                try:
                    value = str(value)
                except TypeError:
                    pass
            if isinstance(value, (basestring)):
                regex = re.compile(r'^\d{4}[- ]?\d\d[- ]?\d\d[T ]\d\d:\d\d:\d\d[.+]\d{2,6}[+-:]\d{2,6}$')
                #regex = re.compile(r'^\d{4}[- ]?\d\d[- ]?\d\d[T ]\d\d:\d\d:\d\d\.\d{3,6}[+-]\d{3,6}$')
                if regex.match(value):
                    #print('Got date formatted string! Converting to datetime!')
                    value = dateutil.parser.parse(value)
                    # Remove the timezone element as excel does not handle
                    value = value.replace(tzinfo=None)

        return value

    def iter_size_changes(self, issue):
        """Yield an IssueSnapshot for each time the issue size changed
        """

        # Find the first size change, if any
        try:
            size_changes = list(filter(lambda h: h.field == 'Story Points',
                                       itertools.chain.from_iterable([c.items for c in issue.changelog.histories])))
        except AttributeError:
            return

        # If we have no size changes and the issue has a current size then a size must have ben specified at issue creation time.
        # Return the size at creation time

        try:
            current_size = issue.fields.__dict__[self.fields['StoryPoints']]
        except:
            current_size = None

        size = (size_changes[0].fromString) if len(size_changes)  else current_size

        # Issue was created
        yield IssueSizeSnapshot(
            change=None,
            key=issue.key,
            date=dateutil.parser.parse(issue.fields.created),
            size=size
        )

        for change in issue.changelog.histories:
            change_date = dateutil.parser.parse(change.created)

            #sizes = list(filter(lambda i: i.field == 'Story Points', change.items))
            #is_resolved = (sizes[-1].to is not None) if len(sizes) > 0 else is_resolved

            for item in change.items:
                if item.field == 'Story Points':
                    # StoryPoints value was changed
                    size = item.toString
                    yield IssueSizeSnapshot(
                        change=item.field,
                        key=issue.key,
                        date=change_date,
                        size=size
                    )



    def iter_changes(self, issue, include_resolution_changes=True):
        """Yield an IssueSnapshot for each time the issue changed status or
        resolution
        """

        is_resolved = False

        # Find the first status change, if any
        try:
            status_changes = list(filter(
                lambda h: h.field == 'status',
                itertools.chain.from_iterable([c.items for c in issue.changelog.histories])))
        except AttributeError:
            return
        last_status = status_changes[0].fromString if len(status_changes) > 0 else issue.fields.status.name
        last_resolution = None

        # Issue was created
        yield IssueSnapshot(
            change=None,
            key=issue.key,
            date=dateutil.parser.parse(issue.fields.created),
            status=last_status,
            resolution=None,
            is_resolved=is_resolved
        )

        for change in issue.changelog.histories:
            change_date = dateutil.parser.parse(change.created)

            resolutions = list(filter(lambda i: i.field == 'resolution', change.items))
            is_resolved = (resolutions[-1].to is not None) if len(resolutions) > 0 else is_resolved

            for item in change.items:
                if item.field == 'status':
                    # Status was changed
                    last_status = item.toString
                    yield IssueSnapshot(
                        change=item.field,
                        key=issue.key,
                        date=change_date,
                        status=last_status,
                        resolution=last_resolution,
                        is_resolved=is_resolved
                    )
                elif item.field == 'resolution':
                    last_resolution = item.toString
                    if include_resolution_changes:
                        yield IssueSnapshot(
                            change=item.field,
                            key=issue.key,
                            date=change_date,
                            status=last_status,
                            resolution=last_resolution,
                            is_resolved=is_resolved
                        )

    # Basic queries

    def find_issues(self, criteria={}, jql=None, order='KEY ASC', verbose=False, changelog=True):
        """Return a list of issues with changelog metadata.

        Searches for the `issue_types`, `project`, `valid_resolutions` and
        'jql_filter' set in the passed-in `criteria` object.

        Pass a JQL string to further qualify the query results.
        """

        query = []

        if criteria.get('project', False):
            query.append('project IN (%s)' % ', '.join(['"%s"' % p for p in criteria['project']]))

        if criteria.get('issue_types', False):
            query.append('issueType IN (%s)' % ', '.join(['"%s"' % t for t in criteria['issue_types']]))

        if criteria.get('valid_resolutions', False):
            query.append('(resolution IS EMPTY OR resolution IN (%s))' % ', '.join(['"%s"' % r for r in criteria['valid_resolutions']]))

        if criteria.get('jql_filter') is not None:
            query.append('(%s)' % criteria['jql_filter'])

        if jql is not None:
            query.append('(%s)' % jql)

        queryString = "%s ORDER BY %s" % (' AND '.join(query), order,)

        if verbose:
            print("Fetching issues with query:", queryString)

        fromRow=0
        issues = []
        while True:
            try:
                if changelog:
                    pageofissues = self.jira.search_issues(queryString, expand='changelog', maxResults=self.settings['max_results'],startAt=fromRow)
                else:
                    pageofissues = self.jira.search_issues(queryString, maxResults=self.settings['max_results'],startAt=fromRow)

                fromRow = fromRow + int(self.settings['max_results'])
                issues += pageofissues
                if verbose:
                    print("Got %s lines per jira query from result starting at line number %s " % (self.settings['max_results'],  fromRow))
                if len(pageofissues)==0:
                    break
            except JIRAError as e:
                print("Jira query error with: {}\n{}".format(queryString, e))
                return []


        if verbose:
            print("Fetched", len(issues), "issues")

        return issues
