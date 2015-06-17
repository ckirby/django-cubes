# -*- coding: utf-8 -*-
import logging
from collections import OrderedDict

from rest_framework.views import APIView
from rest_framework import permissions, status
from rest_framework.response import Response, ErrorResponse
from rest_framework.renderers import TemplateHTMLRenderer

from cubes import __version__, Workspace, SLICER_INFO_KEYS, cut_from_dict
from cubes.errors import NoSuchCubeError
from cubes.calendar import CalendarMemberConverter
from cubes.browser import Cell, cuts_from_string

from django.conf import settings
from django.http import Http404

API_VERSION = 2

__all__ = [
    'ApiVersion', 'Index', 'Info', 'ListCubes',
    'CubeModel', 'CubeAggregation', 'CubeCell',
    'CubeFacts', 'CubeFact', 'CubeMembers',
]


class ApiVersion(APIView):
    permission_classes = (permissions.IsAuthenticated,)

    def get(self, request):
        info = {
            "version": __version__,
            "server_version": __version__,
            "api_version": API_VERSION
        }
        return Response(info)


class CubesView(APIView):
    permission_classes = (permissions.IsAuthenticated,)
    workspace = None

    def initialize_slicer(self):
        if self.workspace is None:
            slicer_config_file = getattr(settings, 'SLICER_CONFIG_FILE', 'slicer.ini')
            self.workspace = Workspace(config=slicer_config_file)

    def get_cube(self, request, cube_name):
        self.initialize_slicer()
        try:
            cube = self.workspace.cube(cube_name, request.user)
        except NoSuchCubeError:
            raise Http404

        return cube

    def get_browser(self, cube):
        return self.workspace.browser(cube)

    def get_cell(self, request, cube, argname="cut", restrict=False):
        """Returns a `Cell` object from argument with name `argname`"""
        converters = {
            "time": CalendarMemberConverter(self.workspace.calendar)
        }

        cuts = []
        for cut_string in request.query_params.getlist(argname):
            cuts += cuts_from_string(
                cube, cut_string, role_member_converters=converters
            )

        if cuts:
            cell = Cell(cube, cuts)
        else:
            cell = None

        if restrict:
            if self.workspace.authorizer:
                cell = self.workspace.authorizer.restricted_cell(
                    request.user, cube=cube, cell=cell
                )
        return cell

    def get_info(self):
        self.initialize_slicer()
        if self.workspace.info:
            info = OrderedDict(self.workspace.info)
        else:
            info = OrderedDict()

        info["cubes_version"] = __version__
        info["timezone"] = self.workspace.calendar.timezone_name
        info["first_weekday"] = self.workspace.calendar.first_weekday
        info["api_version"] = API_VERSION
        return info

    def _handle_pagination_and_order(self, request):
        try:
            page = request.query_params.get('page', None)
        except ValueError:
            page = None
        request.page = page

        try:
            page_size = request.query_params.get('pagesize', None)
        except ValueError:
            page_size = None
        request.page_size = page_size

        # Collect orderings:
        # order is specified as order=<field>[:<direction>]
        order = []
        for orders in request.query_params.getlist('order'):
            for item in orders.split(","):
                split = item.split(":")
                if len(split) == 1:
                    order.append((item, None))
                else:
                    order.append((split[0], split[1]))
        request.order = order

    def initialize_request(self, request, *args, **kwargs):
        request = super(CubesView, self).initialize_request(request, *args, **kwargs)
        self._handle_pagination_and_order(request)
        return request


class Index(CubesView):
    renderer_classes = (TemplateHTMLRenderer,)

    def get(self, request):
        info = self.get_info()
        info['has_about'] = any(key in info for key in SLICER_INFO_KEYS)
        return Response(info, template_name="cubes/index.html")


class Info(CubesView):

    def get(self, request):
        return Response(self.get_info)


class ListCubes(CubesView):

    def get(self, request):
        self.initialize_slicer()
        cube_list = self.workspace.list_cubes(request.user)
        return Response(cube_list)


class CubeModel(CubesView):

    def get(self, request, cube_name):
        cube = self.get_cube(request, cube_name)
        if self.workspace.authorizer:
            hier_limits = self.workspace.authorizer.hierarchy_limits(
                request.user, cube_name
            )
        else:
            hier_limits = None

        model = cube.to_dict(
            expand_dimensions=True,
            with_mappings=False,
            full_attribute_names=True,
            create_label=True,
            hierarchy_limits=hier_limits
        )

        model["features"] = self.workspace.cube_features(cube)
        return Response(model)


class CubeAggregation(CubesView):

    def get(self, request, cube_name):
        cube = self.get_cube(request, cube_name)
        browser = self.get_browser(cube)
        cell = self.get_cell(request, cube, restrict=True)

        # Aggregates
        aggregates = []
        for agg in request.query_params.getlist('aggregates') or []:
            aggregates += agg.split('|')

        drilldown = []
        ddlist = request.query_params.getlist('drilldown')
        if ddlist:
            for ddstring in ddlist:
                drilldown += ddstring.split('|')

        split = self.get_cell(request, cube, argname='split')
        result = browser.aggregate(
            cell,
            aggregates=aggregates,
            drilldown=drilldown,
            split=split,
            page=request.page,
            page_size=request.page_size,
            order=request.order
        )

        return Response(result)


class CubeCell(CubesView):

    def get(self, request, cube_name):
        cube = self.get_cube(request, cube_name)
        browser = self.get_browser(cube)
        cell = self.get_cell(request, cube, restrict=True)
        details = browser.cell_details(cell)

        if not cell:
            cell = Cell(cube)

        cell_dict = cell.to_dict()
        for cut, detail in zip(cell_dict["cuts"], details):
            cut["details"] = detail

        return Response(cell_dict)


class CubeReport(CubesView):

    def make_report(self, request, cube_name):
        cube = self.get_cube(request, cube_name)
        browser = self.get_browser(cube)
        cell = self.get_cell(request, cube, restrict=True)

        report_request = request.DATA
        try:
            queries = report_request["queries"]
        except KeyError:
            message = "Report request does not contain 'queries' key"
            logging.error(message, request=request)
            raise ErrorResponse(status.HTTP_400_BAD_REQUEST, content=message)

        cell_cuts = report_request.get("cell")

        if cell_cuts:
            # Override URL cut with the one in report
            cuts = [cut_from_dict(cut) for cut in cell_cuts]
            cell = Cell(cube, cuts)
            logging.info(
                "Using cell from report specification (URL parameters are ignored)"
            )

            if self.workspace.authorizer:
                cell = self.workspace.authorizer.restricted_cell(
                    request.user, cube=cube, cell=cell
                )
        else:
            if not cell:
                cell = Cell(cube)
            else:
                cell = cell

        report = browser.report(cell, queries)
        return Response(report)

    def get(self, request, cube_name):
        return self.make_report(request, cube_name)

    def post(self, request, cube_name):
        return self.make_report(request, cube_name)


class CubeFacts(CubesView):

    def get(self, request, cube_name):
        cube = self.get_cube(request, cube_name)
        browser = self.get_browser(cube)
        cell = self.get_cell(request, cube, restrict=True)

        # Construct the field list
        fields_str = request.query_params.get('fields')
        if fields_str:
            attributes = cube.get_attributes(fields_str.split(','))
        else:
            attributes = cube.all_attributes

        fields = [attr.ref() for attr in attributes]

        # Get the result
        facts = browser.facts(
            cell,
            fields=fields,
            page=request.page,
            page_size=request.page_size,
            order=request.order
        )

        return Response(facts)


class CubeFact(CubesView):

    def get(self, request, cube_name, fact_id):
        cube = self.get_cube(request, cube_name)
        browser = self.get_browser(cube)
        fact = browser.fact(fact_id)
        return Response(fact)


class CubeMembers(CubesView):

    def get(self, request, cube_name, dimension_name):
        cube = self.get_cube(request, cube_name)
        browser = self.get_browser(cube)
        cell = self.get_cell(request, cube, restrict=True)

        try:
            dimension = cube.dimension(dimension_name)
        except KeyError:
            message = "Dimension '%s' was not found" % dimension_name
            logging.error(message, request=request)
            raise ErrorResponse(status.HTTP_400_BAD_REQUEST, content=message)

        hier_name = request.query_params.get('hierarchy')
        hierarchy = dimension.hierarchy(hier_name)

        depth = request.query_params.get('depth', None)
        level = request.query_params.get('level', None)

        if depth and level:
            message = "Both depth and level provided, use only one (preferably level)"
            logging.error(message, request=request)
            raise ErrorResponse(status.HTTP_400_BAD_REQUEST, content=message)
        elif depth:
            try:
                depth = int(depth)
            except ValueError:
                message = "depth should be an integer"
                logging.error(message, request=request)
                raise ErrorResponse(status.HTTP_400_BAD_REQUEST, content=message)
        elif level:
            depth = hierarchy.level_index(level) + 1
        else:
            depth = len(hierarchy)

        values = browser.members(
            cell,
            dimension,
            depth=depth,
            hierarchy=hierarchy,
            page=request.page,
            page_size=request.page_size
        )

        result = {
            "dimension": dimension.name,
            "hierarchy": hierarchy.name,
            "depth": len(hierarchy) if depth is None else depth,
            "data": values
        }

        return Response(result)
