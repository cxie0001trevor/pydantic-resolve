import asyncio
import inspect
import contextvars
from inspect import iscoroutine
from typing import TypeVar, Dict
from .exceptions import ResolverTargetAttrNotFound, LoaderFieldNotProvidedError, MissingAnnotationError
from typing import Any, Callable, Optional
from pydantic_resolve import core
from .constant import PREFIX, POST_PREFIX
from .util import get_class_field_annotations
from inspect import isclass
from aiodataloader import DataLoader


def LoaderDepend(  # noqa: N802
    dependency: Optional[Callable[..., Any]] = None,
) -> Any:
    return Depends(dependency=dependency)

class Depends:
    def __init__(
        self, 
        dependency: Optional[Callable[..., Any]] = None,
    ):
        self.dependency = dependency

T = TypeVar("T")

class Resolver:
    """
    Entrypoint of a resolve action
    """
    def __init__(self, loader_filters: Optional[Dict[Any, Dict[str, Any]]] = None, ensure_type=False):
        self.ctx = contextvars.ContextVar('pydantic_resolve_internal_context', default={})
        self.loader_filters_ctx = contextvars.ContextVar('pydantic_resolve_internal_filter', default=loader_filters or {})
        self.ensure_type = ensure_type
    
    def exec_method(self, method):
        signature = inspect.signature(method)
        params = {}

        # manage the creation of loader instances
        for k, v in signature.parameters.items():
            if isinstance(v.default, Depends):
                # Base: DataLoader or batch_load_fn
                # transform: data transform function
                Base = v.default.dependency

                # module.kls to avoid same kls name from different module
                cache_key = f'{v.default.dependency.__module__}.{v.default.dependency.__name__}'

                cache_provider = self.ctx.get()
                hit = cache_provider.get(cache_key, None)

                if hit:
                    loader = hit
                else:
                    # create loader instance 
                    if isclass(Base):
                        # if extra transform provides
                        loader = Base()

                        # and pick config from 'loader_filters' param, only for DataClass
                        filter_config_provider = self.loader_filters_ctx.get()
                        filter_config = filter_config_provider.get(Base, {})

                        # class ExampleLoader(DataLoader):
                        #     filtar_x: bool  <--------------- set this
                        #
                        #     async def batch_load_fn(self, keys):
                        #         ....
                        for field in get_class_field_annotations(Base):
                            try:
                                value = filter_config[field]
                                setattr(loader, field, value)
                            except KeyError:
                                raise LoaderFieldNotProvidedError(f'{cache_key}.{field} not found in Resolver()')

                    # build loader from batch_load_fn, filters config is impossible
                    else:
                        loader = DataLoader(batch_load_fn=Base) # type:ignore

                    cache_provider[cache_key] = loader
                    self.ctx.set(cache_provider)
                params[k] = loader
        return method(**params)

    async def resolve_obj(self, target, field):
        item = target.__getattribute__(field)
        target_attr_name = str(field).replace(PREFIX, '')
        val = self.exec_method(item)

        if not hasattr(target, target_attr_name):
            raise ResolverTargetAttrNotFound(f"attribute {target_attr_name} not found")

        if self.ensure_type:
            if not item.__annotations__:
                raise MissingAnnotationError(f'{field}: return annotation is required')

        while iscoroutine(val) or asyncio.isfuture(val):
            val = await val

        # start to resolve
        val = await self.resolve(val)  
        # all children is resolved
        target.__setattr__(target_attr_name, val)


    async def resolve(self, target: T) -> T:
        """ entry: resolve dataclass object or pydantic object / or list in place """

        if isinstance(target, (list, tuple)):
            await asyncio.gather(*[self.resolve(t) for t in target])

        if core.is_acceptable_type(target):
            await asyncio.gather(*[self.resolve_obj(target, field) 
                                   for field in core.iter_over_object_resolvers(target)])

            # execute post methods, accept no params
            for post_key in core.iter_over_object_post_methods(target):
                post_attr_name = post_key.replace(POST_PREFIX, '')
                if not hasattr(target, post_attr_name):
                    raise ResolverTargetAttrNotFound(f"fail to run {post_key}(), attribute {post_attr_name} not found")

                post_method = target.__getattribute__(post_key)
                post_method()

        return target