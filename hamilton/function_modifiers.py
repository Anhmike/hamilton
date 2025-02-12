import functools
import logging
import inspect
import typing
from typing import Dict, Callable, Collection, Tuple, Union, Any, Type, List, NamedTuple

import pandas as pd
import typing_inspect

from hamilton import node
from hamilton.function_modifiers_base import NodeCreator, NodeResolver, NodeExpander, sanitize_function_name, NodeDecorator
from hamilton.models import BaseModel

logger = logging.getLogger(__name__)

"""
Annotations for modifying the way functions get added to the DAG.
All user-facing annotation classes are lowercase as they're meant to be used
as annotations. They are classes to hold state and subclass common functionality.
"""


class InvalidDecoratorException(Exception):
    pass


def get_default_tags(fn: Callable) -> Dict[str, str]:
    """Function that encapsulates default tags on a function.

    :param fn: the function we want to create default tags for.
    :return: a dictionary with str -> str values representing the default tags.
    """
    module_name = inspect.getmodule(fn).__name__
    return {'module': module_name}


class parametrized(NodeExpander):
    def __init__(self, parameter: str, assigned_output: Dict[Tuple[str, str], Any]):
        """Constructor for a modifier that expands a single function into n, each of which
        corresponds to a function in which the parameter value is replaced by that *specific value*.

        :param parameter: Parameter to expand on.
        :param assigned_output: A map of tuple of [parameter names, documentation] to values
        """
        self.parameter = parameter
        self.assigned_output = assigned_output
        for node in assigned_output.keys():
            if not isinstance(node, Tuple):
                raise InvalidDecoratorException(
                    f'assigned_output key is incorrect: {node}. The parameterized decorator needs a dict of '
                    '[name, doc string] -> value to function.')

    def validate(self, fn: Callable):
        """A function is invalid if it does not have the requested parameter.

        :param fn: Function to validate against this annotation.
        :raises: InvalidDecoratorException If the function does not have the requested parameter
        """
        signature = inspect.signature(fn)
        if self.parameter not in signature.parameters.keys():
            raise InvalidDecoratorException(
                f'Annotation is invalid -- no such parameter {self.parameter} in function {fn}')

    def expand_node(self, node_: node.Node, config: Dict[str, Any], fn: Callable) -> Collection[node.Node]:
        """For each parameter value, loop through, partially curry the function, and output a node."""
        input_types = node_.input_types
        nodes = []
        for (node_name, node_doc), value in self.assigned_output.items():
            nodes.append(
                node.Node(
                    node_name,
                    node_.type,
                    node_doc,
                    functools.partial(node_.callable, **{self.parameter: value}),
                    input_types={key: value for key, (value, _) in input_types.items() if key != self.parameter},
                    tags=node_.tags.copy()))
        return nodes


class parametrized_input(NodeExpander):

    def __init__(self, parameter: str, variable_inputs: Dict[str, Tuple[str, str]]):
        """Constructor for a modifier that expands a single function into n, each of which
        corresponds to the specified parameter replaced by a *specific input column*.

        Note this decorator and `@parametrized` are quite similar, except that the input here is another DAG node,
        i.e. column, rather than some specific value.

        The `parameterized_input` allows you keep your code DRY by reusing the same function but replace the inputs
        to create multiple corresponding distinct outputs. The _parameter_ key word argument has to match one of the
        arguments in the function. The rest of the arguments are pulled from items inside the DAG.
        The _assigned_inputs_ key word argument takes in a
        dictionary of input_column -> tuple(Output Name, Documentation string).

        :param parameter: Parameter to expand on.
        :param variable_inputs: A map of tuple of [parameter names, documentation] to values
        """
        logger.warning('`parameterized_input` (singular) is deprecated. It will be removed in a 2.0.0 release. '
                       'Please migrate to using `parameterized_inputs` (plural).')
        self.parameter = parameter
        self.assigned_output = variable_inputs
        for value in variable_inputs.values():
            if not isinstance(value, Tuple):
                raise InvalidDecoratorException(
                    f'assigned_output key is incorrect: {node}. The parameterized decorator needs a dict of '
                    'input column -> [name, description] to function.')

    def expand_node(self, node_: node.Node, config: Dict[str, Any], fn: Callable) -> Collection[node.Node]:
        nodes = []
        input_types = node_.input_types
        for input_column, (node_name, node_description) in self.assigned_output.items():
            specific_inputs = {key: value for key, (value, _) in input_types.items()}
            specific_inputs[input_column] = specific_inputs.pop(self.parameter)  # replace the name with the new function name so we get the right dependencies

            def new_fn(*args, input_column=input_column, **kwargs):
                """This function rewrites what is passed in kwargs to the right kwarg for the function."""
                kwargs = kwargs.copy()
                kwargs[self.parameter] = kwargs.pop(input_column)
                return node_.callable(*args, **kwargs)

            nodes.append(
                node.Node(
                    node_name,
                    node_.type,
                    node_description,
                    new_fn,
                    input_types=specific_inputs,
                    tags=node_.tags.copy()))
        return nodes

    def validate(self, fn: Callable):
        """A function is invalid if it does not have the requested parameter.

        :param fn: Function to validate against this annotation.
        :raises: InvalidDecoratorException If the function does not have the requested parameter
        """
        signature = inspect.signature(fn)
        if self.parameter not in signature.parameters.keys():
            raise InvalidDecoratorException(
                f'Annotation is invalid -- no such parameter {self.parameter} in function {fn}')


class parameterized_inputs(NodeExpander):
    RESERVED_KWARG = 'output_name'

    def __init__(self, **parameterization: Dict[str, Dict[str, str]]):
        """Constructor for a modifier that expands a single function into n, each of which corresponds to replacing
        some subset of the specified parameters with specific inputs.

        Note this decorator and `@parametrized_input` are similar, except this one allows multiple
        parameters to be mapped to multiple function arguments (and it fixes the spelling mistake).

        `parameterized_inputs` allows you keep your code DRY by reusing the same function but replace the inputs
        to create multiple corresponding distinct outputs. We see here that `parameterized_inputs` allows you to keep
        your code DRY by reusing the same function to create multiple distinct outputs. The key word arguments passed
        have to have the following structure:
            > OUTPUT_NAME = Mapping of function argument to input that should go into it.
        The documentation for the output is taken from the function. The documentation string can be templatized with
        the parameter names of the function and the reserved value `output_name` - those will be replaced with the
        corresponding values from the parameterization.

        :param **parameterization: kwargs of output name to dict of parameter mappings.
        """
        self.parametrization = parameterization
        if not parameterization:
            raise ValueError(f'Cannot pass empty/None dictionary to parameterized_inputs')
        for output, mappings in parameterization.items():
            if not mappings:
                raise ValueError(f'Error, {output} has a none/empty dictionary mapping. Please fill it.')

    def expand_node(self, node_: node.Node, config: Dict[str, Any], fn: Callable) -> Collection[node.Node]:
        nodes = []
        input_types = node_.input_types
        for output_name, mapping in self.parametrization.items():
            node_name = output_name
            # output_name is a reserved kwarg name.
            node_description = self.format_doc_string(node_.documentation, output_name, **mapping)
            specific_inputs = {key: value for key, (value, _) in input_types.items()}
            for func_param, replacement_param in mapping.items():
                logger.info(f'For function {node_.name}: mapping {replacement_param} to {func_param}.')
                # replace the name with the new function name so we get the right dependencies
                specific_inputs[replacement_param] = specific_inputs.pop(func_param)

            def new_fn(*args, inputs=mapping, **kwargs):
                """This function rewrites what is passed in kwargs to the right kwarg for the function."""
                kwargs = kwargs.copy()
                for func_param, replacement_param in inputs.items():
                    kwargs[func_param] = kwargs.pop(replacement_param)
                return node_.callable(*args, **kwargs)

            nodes.append(
                node.Node(
                    node_name,
                    node_.type,
                    node_description,
                    new_fn,
                    input_types=specific_inputs,
                    tags=node_.tags.copy()))
        return nodes

    def format_doc_string(self, doc: str, output_name: str, **params: Dict[str, str]) -> str:
        """Helper function to format a function documentation string.

        :param doc: the string template to format
        :param output_name: the output name of the function
        :param params: the parameter mappings
        :return: formatted string
        :raises: KeyError if there is a template variable missing from the parameter mapping.
        """
        return doc.format(**{self.RESERVED_KWARG: output_name}, **params)

    def validate(self, fn: Callable):
        """A function is invalid if it does not have the requested parameter.

        :param fn: Function to validate against this annotation.
        :raises: InvalidDecoratorException If the function does not have the requested parameter
        """
        signature = inspect.signature(fn)
        func_param_name_set = set(signature.parameters.keys())
        try:
            for output_name, mappings in self.parametrization.items():
                self.format_doc_string(fn.__doc__, output_name, **mappings)
        except KeyError as e:
            raise InvalidDecoratorException(f'Function docstring templating is incorrect. '
                                            f'Please fix up the docstring {fn.__module__}.{fn.__name__}.') from e

        if self.RESERVED_KWARG in func_param_name_set:
            raise InvalidDecoratorException(
                f'Error function {fn.__module__}.{fn.__name__} cannot have `{self.RESERVED_KWARG}` '
                f'as a parameter it is reserved.')
        missing_params = set()
        for output_name, mappings in self.parametrization.items():
            for func_name, replacement_name in mappings.items():
                if func_name not in func_param_name_set:
                    missing_params.add(func_name)
        if missing_params:
            raise InvalidDecoratorException(
                f'Annotation is invalid -- No such parameter(s) {missing_params} in function '
                f'{fn.__module__}.{fn.__name__}.')


class extract_columns(NodeExpander):

    def __init__(self, *columns: Union[Tuple[str, str], str], fill_with: Any = None):
        """Constructor for a modifier that expands a single function into the following nodes:
        - n functions, each of which take in the original dataframe and output a specific column
        - 1 function that outputs the original dataframe

        :param columns: Columns to extract, that can be a list of tuples of (name, documentation) or just names.
        :param fill_with: If you want to extract a column that doesn't exist, do you want to fill it with a default value?
        Or do you want to error out? Leave empty/None to error out, set fill_value to dynamically create a column.
        """
        if not columns:
            raise InvalidDecoratorException('Error empty arguments passed to extract_columns decorator.')
        elif isinstance(columns[0], list):
            raise InvalidDecoratorException('Error list passed in. Please `*` in front of it to expand it.')
        self.columns = columns
        self.fill_with = fill_with

    def validate(self, fn: Callable):
        """A function is invalid if it does not output a dataframe.

        :param fn: Function to validate.
        :raises: InvalidDecoratorException If the function does not output a Dataframe
        """
        output_type = inspect.signature(fn).return_annotation
        if not issubclass(output_type, pd.DataFrame):
            raise InvalidDecoratorException(
                f'For extracting columns, output type must be pandas dataframe, not: {output_type}')

    def expand_node(self, node_: node.Node, config: Dict[str, Any], fn: Callable) -> Collection[node.Node]:
        """For each column to extract, output a node that extracts that column. Also, output the original dataframe
        generator.

        :param config:
        :param fn: Function to extract columns from. Must output a dataframe.
        :return: A collection of nodes --
                one for the original dataframe generator, and another for each column to extract.
        """
        fn = node_.callable
        base_doc = node_.documentation

        @functools.wraps(fn)
        def df_generator(*args, **kwargs):
            df_generated = fn(*args, **kwargs)
            if self.fill_with is not None:
                for col in self.columns:
                    if col not in df_generated:
                        df_generated[col] = self.fill_with
            return df_generated

        output_nodes = [node.Node(node_.name,
                                  typ=pd.DataFrame,
                                  doc_string=base_doc,
                                  callabl=df_generator,
                                  tags=node_.tags.copy())]

        for column in self.columns:
            doc_string = base_doc  # default doc string of base function.
            if isinstance(column, Tuple):  # Expand tuple into constituents
                column, doc_string = column

            def extractor_fn(column_to_extract: str = column, **kwargs) -> pd.Series:  # avoiding problems with closures
                df = kwargs[node_.name]
                if column_to_extract not in df:
                    raise InvalidDecoratorException(f'No such column: {column_to_extract} produced by {node_.name}. '
                                                    f'It only produced {str(df.columns)}')
                return kwargs[node_.name][column_to_extract]

            output_nodes.append(
                node.Node(column, pd.Series, doc_string, extractor_fn,
                          input_types={node_.name: pd.DataFrame}, tags=node_.tags.copy()))
        return output_nodes


class extract_fields(NodeExpander):
    """Extracts fields from a dictionary of output."""

    def __init__(self, fields: dict, fill_with: Any = None):
        """Constructor for a modifier that expands a single function into the following nodes:
        - n functions, each of which take in the original dict and output a specific field
        - 1 function that outputs the original dict

        :param fields: Fields to extract. A dict of 'field_name' -> 'field_type'.
        :param fill_with: If you want to extract a field that doesn't exist, do you want to fill it with a default value?
        Or do you want to error out? Leave empty/None to error out, set fill_value to dynamically create a field value.
        """
        if not fields:
            raise InvalidDecoratorException('Error an empty dict, or no dict, passed to extract_fields decorator.')
        elif not isinstance(fields, dict):
            raise InvalidDecoratorException(f'Error, please pass in a dict, not {type(fields)}')
        else:
            errors = []
            for field, field_type in fields.items():
                if not isinstance(field, str):
                    errors.append(f'{field} is not a string. All keys must be strings.')
                if not isinstance(field_type, type):
                    errors.append(f'{field} does not declare a type. Instead it passes {field_type}.')

            if errors:
                raise InvalidDecoratorException(f'Error, found these {errors}. '
                                                f'Please pass in a dict of string to types. ')
        self.fields = fields
        self.fill_with = fill_with

    def validate(self, fn: Callable):
        """A function is invalid if it is not annotated with a dict or typing.Dict return type.

        :param fn: Function to validate.
        :raises: InvalidDecoratorException If the function is not annotated with a dict or typing.Dict type as output.
        """
        output_type = inspect.signature(fn).return_annotation
        if typing_inspect.is_generic_type(output_type):
            base = typing_inspect.get_origin(output_type)
            if base == dict or base == typing.Dict:  # different python versions return different things 3.7+ vs 3.6.
                pass
            else:
                raise InvalidDecoratorException(
                    f'For extracting fields, output type must be a dict or typing.Dict, not: {output_type}')
        elif output_type == dict:
            pass
        else:
            raise InvalidDecoratorException(
                f'For extracting fields, output type must be a dict or typing.Dict, not: {output_type}')

    def expand_node(self, node_: node.Node, config: Dict[str, Any], fn: Callable) -> Collection[node.Node]:
        """For each field to extract, output a node that extracts that field. Also, output the original TypedDict
        generator.

        :param node_:
        :param config:
        :param fn: Function to extract columns from. Must output a dataframe.
        :return: A collection of nodes --
                one for the original dataframe generator, and another for each column to extract.
        """
        fn = node_.callable
        base_doc = node_.documentation

        @functools.wraps(fn)
        def dict_generator(*args, **kwargs):
            dict_generated = fn(*args, **kwargs)
            if self.fill_with is not None:
                for field in self.fields:
                    if field not in dict_generated:
                        dict_generated[field] = self.fill_with
            return dict_generated

        output_nodes = [node.Node(node_.name, typ=dict, doc_string=base_doc, callabl=dict_generator, tags=node_.tags.copy())]

        for field, field_type in self.fields.items():
            doc_string = base_doc  # default doc string of base function.

            def extractor_fn(field_to_extract: str = field, **kwargs) -> field_type:  # avoiding problems with closures
                dt = kwargs[node_.name]
                if field_to_extract not in dt:
                    raise InvalidDecoratorException(f'No such field: {field_to_extract} produced by {node_.name}. '
                                                    f'It only produced {list(dt.keys())}')
                return kwargs[node_.name][field_to_extract]

            output_nodes.append(
                node.Node(field, field_type, doc_string, extractor_fn, input_types={node_.name: dict}, tags=node_.tags.copy()))
        return output_nodes


# the following are empty functions that we can compare against to ensure that @does uses an empty function
def _empty_function():
    pass


def _empty_function_with_docstring():
    """Docstring for an empty function"""
    pass


def ensure_function_empty(fn: Callable):
    """
    Ensures that a function is empty. This is strict definition -- the function must have only one line (and
    possibly a docstring), and that line must say "pass".
    """
    if fn.__code__.co_code not in {_empty_function.__code__.co_code,
                                   _empty_function_with_docstring.__code__.co_code}:
        raise InvalidDecoratorException(f'Function: {fn.__name__} is not empty. Must have only one line that '
                                        f'consists of "pass"')


class does(NodeCreator):
    def __init__(self, replacing_function: Callable):
        """
        Constructor for a modifier that replaces the annotated functions functionality with something else.
        Right now this has a very strict validation requirements to make compliance with the framework easy.
        """
        self.replacing_function = replacing_function

    @staticmethod
    def ensure_output_types_match(fn: Callable, todo: Callable):
        """
        Ensures that the output types of two functions match.
        """
        annotation_fn = inspect.signature(fn).return_annotation
        annotation_todo = inspect.signature(todo).return_annotation
        if not issubclass(annotation_todo, annotation_fn):
            raise InvalidDecoratorException(f'Output types: {annotation_fn} and {annotation_todo} are not compatible')

    @staticmethod
    def ensure_function_kwarg_only(fn: Callable):
        """
        Ensures that a function is kwarg only. Meaning that it only has one parameter similar to **kwargs.
        """
        parameters = inspect.signature(fn).parameters
        if len(parameters) > 1:
            raise InvalidDecoratorException('Too many parameters -- for now @does can only use **kwarg functions. '
                                            f'Found params: {parameters}')
        (_, parameter), = parameters.items()
        if not parameter.kind == inspect.Parameter.VAR_KEYWORD:
            raise InvalidDecoratorException(f'Must have only one parameter, and that parameter must be a **kwargs '
                                            f'parameter. Instead, found: {parameter}')

    def validate(self, fn: Callable):
        """
        Validates that the function:
        - Is empty (we don't want to be overwriting actual code)
        - is keyword argument only (E.G. has just **kwargs in its argument list)
        :param fn: Function to validate
        :raises: InvalidDecoratorException
        """
        ensure_function_empty(fn)
        does.ensure_function_kwarg_only(self.replacing_function)
        does.ensure_output_types_match(fn, self.replacing_function)

    def generate_node(self, fn: Callable, config) -> node.Node:
        """
        Returns one node which has the replaced functionality
        :param fn:
        :param config:
        :return:
        """
        fn_signature = inspect.signature(fn)
        return node.Node(
            fn.__name__,
            typ=fn_signature.return_annotation,
            doc_string=fn.__doc__ if fn.__doc__ is not None else '',
            callabl=self.replacing_function,
            input_types={key: value.annotation for key, value in fn_signature.parameters.items()},
            tags=get_default_tags(fn))


class dynamic_transform(NodeCreator):
    def __init__(self, transform_cls: Type[BaseModel], config_param: str, **extra_transform_params):
        """Constructs a model. Takes in a model_cls, which has to have a parameter."""
        self.transform_cls = transform_cls
        self.config_param = config_param
        self.extra_transform_params = extra_transform_params

    def validate(self, fn: Callable):
        """Validates that the model works with the function -- ensures:
        1. function has no code
        2. function has no parameters
        3. function has series as a return type
        :param fn: Function to validate
        :raises InvalidDecoratorException if the model is not valid.
        """

        ensure_function_empty(fn)  # it has to look exactly
        signature = inspect.signature(fn)
        if not issubclass(signature.return_annotation, pd.Series):
            raise InvalidDecoratorException('Models must declare their return type as a pandas Series')
        if len(signature.parameters) > 0:
            raise InvalidDecoratorException('Models must have no parameters -- all are passed in through the config')

    def generate_node(self, fn: Callable, config: Dict[str, Any] = None) -> node.Node:
        if self.config_param not in config:
            raise InvalidDecoratorException(f'Configuration has no parameter: {self.config_param}. Did you define it? If so did you spell it right?')
        fn_name = fn.__name__
        transform = self.transform_cls(config[self.config_param], fn_name, **self.extra_transform_params)
        return node.Node(
            name=fn_name,
            typ=inspect.signature(fn).return_annotation,
            doc_string=fn.__doc__,
            callabl=transform.compute,
            input_types={dep: pd.Series for dep in transform.get_dependents()},
            tags=get_default_tags(fn))


class model(dynamic_transform):
    """Model, same as a dynamic transform"""

    def __init__(self, model_cls, config_param: str, **extra_model_params):
        super(model, self).__init__(transform_cls=model_cls, config_param=config_param, **extra_model_params)


class config(NodeResolver):
    """Decorator class that resolves a node's function based on  some configuration variable
    Currently, functions that exist in all configurations have to be disjoint.
    E.G. for every config.when(), you can have a config.when_not() that filters the opposite.
    That said, you can have functions that *only* exist in certain configurations without worrying about it.
    """

    def __init__(self, resolves: Callable[[Dict[str, Any]], bool], target_name: str = None):
        self.does_resolve = resolves
        self.target_name = target_name

    def _get_function_name(self, fn: Callable) -> str:
        if self.target_name is not None:
            return self.target_name
        return sanitize_function_name(fn.__name__)

    def resolve(self, fn, configuration: Dict[str, Any]) -> Callable:
        if not self.does_resolve(configuration):
            return None
        fn.__name__ = self._get_function_name(fn)  # TODO -- copy function to not mutate it
        return fn

    def validate(self, fn):
        if fn.__name__.endswith('__'):
            raise InvalidDecoratorException('Config will always use the portion of the function name before the last __. For example, signups__v2 will map to signups, whereas')

    @staticmethod
    def when(name=None, **key_value_pairs) -> 'config':
        """Yields a decorator that resolves the function if all keys in the config are equal to the corresponding value

        :param key_value_pairs: Keys and corresponding values to look up in the config
        :return: a configuration decorator
        """

        def resolves(configuration: Dict[str, Any]) -> bool:
            return all(value == configuration.get(key) for key, value in key_value_pairs.items())

        return config(resolves, target_name=name)

    @staticmethod
    def when_not(name=None, **key_value_pairs: Any) -> 'config':
        """Yields a decorator that resolves the function if none keys in the config are equal to the corresponding value

        :param key_value_pairs: Keys and corresponding values to look up in the config
        :return: a configuration decorator
        """

        def resolves(configuration: Dict[str, Any]) -> bool:
            return all(value != configuration.get(key) for key, value in key_value_pairs.items())

        return config(resolves, target_name=name)

    @staticmethod
    def when_in(name=None, **key_value_group_pairs: Collection[Any]) -> 'config':
        """Yields a decorator that resolves the function if all of the keys are equal to one of items in the list of values.

        :param key_value_group_pairs: pairs of key-value mappings where the value is a list of possible values
        :return: a configuration decorator
        """

        def resolves(configuration: Dict[str, Any]) -> bool:
            return all(configuration.get(key) in value for key, value in key_value_group_pairs.items())

        return config(resolves, target_name=name)

    @staticmethod
    def when_not_in(**key_value_group_pairs: Collection[Any]) -> 'config':
        """Yields a decorator that resolves the function only if none of the keys are in the list of values.

        :param key_value_group_pairs: pairs of key-value mappings where the value is a list of possible values
        :return: a configuration decorator

        :Example:

        @config.when_not_in(business_line=["mens","kids"], region=["uk"])
        def LEAD_LOG_BASS_MODEL_TIMES_TREND(TREND_BSTS_WOMENS_ACQUISITIONS: pd.Series,
                                    LEAD_LOG_BASS_MODEL_SIGNUPS_NON_REFERRAL: pd.Series) -> pd.Series:

        above will resolve for config has {"business_line": "womens", "region": "us"},
        but not for configs that have {"business_line": "mens", "region": "us"}, {"business_line": "kids", "region": "us"},
        or {"region": "uk"}

        .. seealso:: when_not
        """

        def resolves(configuration: Dict[str, Any]) -> bool:
            return all(configuration.get(key) not in value for key, value in key_value_group_pairs.items())

        return config(resolves)


class tag(NodeDecorator):
    """Decorator class that adds a tag to a node. Tags take the form of key/value pairings.
    Tags can have dots to specify namespaces (keys with dots), but this is usually reserved for special cases
    (E.G. subdecorators) that utilize them. Usually one will pass in tags as kwargs, so we expect tags to
    be un-namespaced in most uses.

    That is using:
    > @tag(my_tag='tag_value')
    > def my_function(...) -> ...:
    is un-namespaced because you cannot put a `.` in the keyword part (the part before the '=').

    But using:
    > @tag(**{'my.tag': 'tag_value'})
    > def my_function(...) -> ...:
    allows you to add dots that allow you to namespace your tags.

    Currently, tag values are restricted to allowing strings only, although we may consider changing the in the future
    (E.G. thinking of lists).

    Hamilton also reserves the right to change the following:
    * adding purely positional arguments
    * not allowing users to use a certain set of top-level prefixes (E.G. any tag where the top level is one of the
      values in RESERVED_TAG_PREFIX).

    Example usage:
    > @tag(foo='bar', a_tag_key='a_tag_value', **{'namespace.tag_key': 'tag_value'})
    > def my_function(...) -> ...:
    >   ...
    """

    RESERVED_TAG_NAMESPACES = [
        'hamilton',
        'data_quality',
        'gdpr',
        'ccpa',
        'dag',
        'module',
    ]  # Anything that starts with any of these is banned, the framework reserves the right to manage it

    def __init__(self, **tags: str):
        """Constructor for adding tag annotations to a function.

        :param tags: the keys are always going to be strings, so the type annotation here means the values are strings.
            Implicitly this is `Dict[str, str]` but the PEP guideline is to only annotate it with `str`.
        """
        self.tags = tags

    def decorate_node(self, node_: node.Node) -> node.Node:
        """Decorates the nodes produced by this with the specified tags

        :param node_: Node to decorate
        :return: Copy of the node, with tags assigned
        """
        unioned_tags = self.tags.copy()
        unioned_tags.update(node_.tags)
        return node.Node(
            name=node_.name,
            typ=node_.type,
            doc_string=node_.documentation,
            callabl=node_.callable,
            node_source=node_.node_source,
            input_types=node_.input_types,
            tags=unioned_tags)

    @staticmethod
    def _key_allowed(key: str) -> bool:
        """Validates that a tag key is allowed. Rules are:
        1. It must not be empty
        2. It can have dots, which specify a hierarchy of order
        3. All components, when split by dots, must be valid python identifiers
        4. It cannot utilize a reserved namespace

        :param key: The key to validate
        :return: True if it is valid, False if not
        """
        key_components = key.split('.')
        if len(key_components) == 0:
            # empty string...
            return False
        if key_components[0] in tag.RESERVED_TAG_NAMESPACES:
            # Reserved prefixes
            return False
        for key in key_components:
            if not key.isidentifier():
                return False
        return True

    @staticmethod
    def _value_allowed(value: Any) -> bool:
        """Validates that a tag value is allowed. Rules are only that it must be a string.

        :param value: Value to validate
        :return: True if it is valid, False otherwise
        """
        if not isinstance(value, str):
            return False
        return True

    def validate(self, fn: Callable):
        """Validates the decorator. In this case that the set of tags produced is final.

        :param fn: Function that the decorator is called on.
        :raises ValueError: if the specified tags contains invalid ones
        """
        bad_tags = set()
        for key, value in self.tags.items():
            if (not tag._key_allowed(key)) or (not tag._value_allowed(value)):
                bad_tags.add((key, value))
        if bad_tags:
            bad_tags_formatted = ','.join([f'{key}={value}' for key, value in bad_tags])
            raise InvalidDecoratorException(f'The following tags are invalid as tags: {bad_tags_formatted} '
                                            'Tag keys can be split by ., to represent a hierarchy, '
                                            'but each element of the hierarchy must be a valid python identifier. '
                                            'Paths components also cannot be empty. '
                                            'The value can be anything. Note that the following top-level prefixes are '
                                            f'reserved as well: {self.RESERVED_TAG_NAMESPACES}')
