# This code is part of Qiskit.
#
# (C) Copyright IBM 2023.
#
# This code is licensed under the Apache License, Version 2.0. You may
# obtain a copy of this license in the LICENSE.txt file in the root directory
# of this source tree or at http://www.apache.org/licenses/LICENSE-2.0.
#
# Any modifications or derivative works of this code must retain this
# copyright notice, and modified files need to carry a notice indicating
# that they have been altered from the originals.

"""Base class for Qiskit Runtime primitives."""

from __future__ import annotations
from abc import ABC, abstractmethod
from typing import Dict, Optional, Union, TypeVar, Generic, Type
import logging
from dataclasses import asdict, replace

from qiskit.primitives.containers.estimator_pub import EstimatorPub
from qiskit.primitives.containers.sampler_pub import SamplerPub
from qiskit.providers.backend import BackendV2

from .options.options import BaseOptions, OptionsV2
from .options.utils import merge_options_v2
from .runtime_job_v2 import RuntimeJobV2
from .ibm_backend import IBMBackend

from .utils import (
    validate_isa_circuits,
    validate_no_dd_with_dynamic_circuits,
    validate_rzz_pubs,
)
from .utils.default_session import get_cm_session
from .utils.utils import is_simulator
from .utils.deprecation import issue_deprecation_msg
from .constants import DEFAULT_DECODERS
from .qiskit_runtime_service import QiskitRuntimeService
from .fake_provider.local_service import QiskitRuntimeLocalService

# pylint: disable=unused-import,cyclic-import
from .session import Session
from .batch import Batch

logger = logging.getLogger(__name__)
OptionsT = TypeVar("OptionsT", bound=BaseOptions)


def _get_mode_service_backend(mode: Optional[Union[BackendV2, Session, Batch]] = None) -> tuple[
    Union[Session, Batch, None],
    Union[QiskitRuntimeService, QiskitRuntimeLocalService, None],
    Union[BackendV2, None],
]:
    """
    A utility function that returns mode, service, and backend for a given execution mode.

    Args:
        mode: The execution mode used to make the primitive query. It can be

            * A :class:`Backend` if you are using job mode.
            * A :class:`Session` if you are using session execution mode.
            * A :class:`Batch` if you are using batch execution mode.
    """

    if isinstance(mode, (Session, Batch)):
        return mode, mode.service, mode._backend
    elif isinstance(mode, IBMBackend):  # type: ignore[unreachable]
        if get_cm_session():
            logger.warning(
                "A backend was passed in as the mode but a session context manager "
                "is open so this job will run inside this session/batch "
                "instead of in job mode."
            )
            if get_cm_session()._backend != mode:
                raise ValueError(
                    "The backend passed in to the primitive is different from the session backend. "
                    "Please check which backend you intend to use or leave the mode parameter "
                    "empty to use the session backend."
                )
            return get_cm_session(), mode.service, mode
        return None, mode.service, mode
    elif isinstance(mode, BackendV2):
        return None, QiskitRuntimeLocalService(), mode
    elif mode is not None:  # type: ignore[unreachable]
        raise ValueError("mode must be of type Backend, Session, Batch or None")
    elif get_cm_session():
        mode = get_cm_session()
        service = mode.service
        backend = mode._backend

        return mode, service, backend
    else:
        raise ValueError("A backend or session must be specified.")


class BasePrimitiveV2(ABC, Generic[OptionsT]):
    """Base class for Qiskit Runtime primitives."""

    _options_class: Type[OptionsT] = OptionsV2  # type: ignore[assignment]
    version = 2

    def __init__(
        self,
        mode: Optional[Union[BackendV2, Session, Batch, str]] = None,
        options: Optional[Union[Dict, OptionsT]] = None,
    ):
        """Initializes the primitive.

        Args:
            mode: The execution mode used to make the primitive query. It can be

                * A :class:`Backend` if you are using job mode.
                * A :class:`Session` if you are using session execution mode.
                * A :class:`Batch` if you are using batch execution mode.

            options: Primitive options, see :class:`qiskit_ibm_runtime.options.EstimatorOptions`
                and :class:`qiskit_ibm_runtime.options.SamplerOptions` for detailed description
                on estimator and sampler options, respectively.

        Raises:
            ValueError: Invalid arguments are given.
        """
        self._mode, self._service, self._backend = _get_mode_service_backend(mode)
        self._set_options(options)

    def _run(self, pubs: Union[list[EstimatorPub], list[SamplerPub]]) -> RuntimeJobV2:
        """Run the primitive.

        Args:
            pubs: Inputs PUBs to pass to the primitive.

        Returns:
            Submitted job.
        """
        primitive_inputs = {"pubs": pubs}
        options_dict = asdict(self.options)
        self._validate_options(options_dict)
        primitive_options = self._options_class._get_program_inputs(options_dict)
        primitive_inputs.update(primitive_options)
        runtime_options = self._options_class._get_runtime_options(options_dict)

        validate_no_dd_with_dynamic_circuits([pub.circuit for pub in pubs], self.options)
        if self._backend:
            if not is_simulator(self._backend):
                validate_rzz_pubs(pubs)
            for pub in pubs:
                if getattr(self._backend, "target", None) and not is_simulator(self._backend):
                    validate_isa_circuits([pub.circuit], self._backend.target)

                if isinstance(self._backend, IBMBackend):
                    self._backend.check_faulty(pub.circuit)

        logger.info("Submitting job using options %s", primitive_options)

        if options_dict.get("environment", {}).get("callback", None):
            issue_deprecation_msg(
                msg="The 'callback' option is deprecated",
                version="0.38.0",
                remedy="This option will have no effect since interim "
                "results streaming was removed in a previous release.",
                stacklevel=3,
            )

        # Batch or Session
        if self._mode:
            return self._mode._run(
                program_id=self._program_id(),
                inputs=primitive_inputs,
                options=runtime_options,
                callback=options_dict.get("environment", {}).get("callback", None),
                result_decoder=DEFAULT_DECODERS.get(self._program_id()),
            )

        if self._backend:
            if get_cm_session():
                logger.warning(
                    "Even though a session/batch context manager is open this job will run in job mode "
                    "because the %s primitive was initialized outside the context manager. "
                    "Move the %s initialization inside the context manager to run in a session/batch.",
                    self._program_id(),
                    self._program_id(),
                )
            runtime_options["backend"] = self._backend
            if "instance" not in runtime_options and isinstance(self._backend, IBMBackend):
                runtime_options["instance"] = self._backend._instance

        if isinstance(self._service, QiskitRuntimeService):
            return self._service._run(
                program_id=self._program_id(),
                options=runtime_options,
                inputs=primitive_inputs,
                callback=options_dict.get("environment", {}).get("callback", None),
                result_decoder=DEFAULT_DECODERS.get(self._program_id()),
            )

        return self._service._run(
            program_id=self._program_id(),  # type: ignore[arg-type]
            options=runtime_options,
            inputs=primitive_inputs,
        )

    @property
    def mode(self) -> Optional[Session | Batch]:
        """Return the execution mode used by this primitive.

        Returns:
            Mode used by this primitive, or ``None`` if an execution mode is not used.
        """
        return self._mode

    @property
    def options(self) -> OptionsT:
        """Return options"""
        return self._options

    def backend(self) -> BackendV2:
        """Return the backend the primitive query will be run on."""
        return self._backend

    def _set_options(self, options: Optional[Union[Dict, OptionsT]] = None) -> None:
        """Set options."""
        if options is None:
            self._options = self._options_class()
        elif isinstance(options, dict):
            default_options = self._options_class()
            self._options = self._options_class(**merge_options_v2(default_options, options))

        elif isinstance(options, self._options_class):
            self._options = replace(options)
        else:
            raise TypeError(
                f"Invalid 'options' type. It can only be a dictionary of {self._options_class}"
            )

    @abstractmethod
    def _validate_options(self, options: dict) -> None:
        """Validate that program inputs (options) are valid

        Raises:
            ValueError: if resilience_level is out of the allowed range.
        """
        raise NotImplementedError()

    @classmethod
    @abstractmethod
    def _program_id(cls) -> str:
        """Return the program ID."""
        raise NotImplementedError()
