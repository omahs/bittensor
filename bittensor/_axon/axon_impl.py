""" Implementation of Axon, services Forward and Backward requests from other neurons.
"""
# The MIT License (MIT)
# Copyright © 2021 Yuma Rao

# Permission is hereby granted, free of charge, to any person obtaining a copy of this software and associated 
# documentation files (the “Software”), to deal in the Software without restriction, including without limitation 
# the rights to use, copy, modify, merge, publish, distribute, sublicense, and/or sell copies of the Software, 
# and to permit persons to whom the Software is furnished to do so, subject to the following conditions:

# The above copyright notice and this permission notice shall be included in all copies or substantial portions of 
# the Software.

# THE SOFTWARE IS PROVIDED “AS IS”, WITHOUT WARRANTY OF ANY KIND, EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO
# THE WARRANTIES OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL 
# THE AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION 
# OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER 
# DEALINGS IN THE SOFTWARE.

import sys
import time as clock
from types import SimpleNamespace
from typing import List, Tuple, Callable

import torch
import grpc
import wandb
import pandas
from loguru import logger
import torch.nn.functional as F
import concurrent

import bittensor
import bittensor.utils.stats as stat_utils

logger = logger.opt(colors=True)

class Axon( bittensor.grpc.BittensorServicer ):
    r""" Services Forward and Backward requests from other neurons.
    """
    def __init__( 
        self, 
        wallet: 'bittensor.wallet',
        ip: str,
        port: int,
        server: 'grpc._Server',
        forward: 'Callable',
        backward: 'Callable',
        synapses: dict,
        priority:  'Callable' = None,
        priority_threadpool: 'bittensor.prioritythreadpool' = None,
        forward_timeout: int = None,
        backward_timeout: int = None,
    ):
        r""" Initializes a new Axon tensor processing endpoint.
            
            Args:
                config (:obj:`bittensor.Config`, `required`): 
                    bittensor.axon.config()
                wallet (:obj:`bittensor.wallet`, `required`):
                    bittensor wallet with hotkey and coldkeypub.
                server (:obj:`grpc._Server`, `required`):
                    Grpc server endpoint.
                forward (:obj:list of `callable`, `optional`):
                    list of functions which is called on forward requests.
                backward (:obj:list of `callable`, `optional`):
                    list of functions which is called on backward requests.
                priority (:obj:`callable`, `optional`):
                    function to assign priority on requests.
                priority_threadpool (:obj:`bittensor.prioritythreadpool`, `optional`):
                    bittensor priority_threadpool.                
        """
        self.ip = ip
        self.port = port
        self.wallet = wallet
        self.server = server
        self.forward_callback = forward if forward != None else self.default_forward_callback
        self.backward_callback = backward if backward != None else self.default_backward_callback
        self.forward_timeout = forward_timeout
        self.backward_timeout = backward_timeout
        self.synapse_callbacks = synapses
        self.stats = self._init_stats()
        self.started = None
        self.optimizer = None
        
        # -- Priority 
        self.priority = priority 
        self.priority_threadpool= priority_threadpool

    def __str__(self) -> str:
        return "Axon({}, {}, {}, {})".format( self.ip, self.port, self.wallet.hotkey.ss58_address, "started" if self.started else "stopped")

    def __repr__(self) -> str:
        return self.__str__()

    def Forward(self, request: bittensor.proto.TensorMessage, context: grpc.ServicerContext) -> bittensor.proto.TensorMessage:
        r""" The function called by remote GRPC Forward requests from other neurons.
            Forward is equivalent to a 'forward' pass through a neural network.
            After checking request validity, this function passes the request to the nucleus for processing.
            See :obj:`bittensor.proto.ReturnCode` for all possible return codes.
            
            Args:
                request (:obj:`bittensor.proto`, `required`): 
                    Tensor request proto.
                context (:obj:`grpc.ServicerContext`, `required`): 
                    grpc server context.
            
            Returns:
                response (bittensor.proto.TensorMessage): 
                    proto response carring the nucleus forward output or None under failure.
        """
        forward_response_tensors, code, synapses = self._forward( request )
        # TODO(eugene) Shouldnt we be signing these responses ?
        response = bittensor.proto.TensorMessage(
            version = bittensor.__version_as_int__, 
            hotkey = self.wallet.hotkey.ss58_address, 
            return_code = code,
            tensors = forward_response_tensors if forward_response_tensors is not None else [],
            requires_grad = request.requires_grad,
            synapses = synapses,
        )
        return response

    def Backward( self, request: bittensor.proto.TensorMessage, context: grpc.ServicerContext ) -> bittensor.proto.TensorMessage:
        r""" The function called by remote GRPC Backward requests from other neurons.
            Backward is equivalent to a 'backward' gradient descent pass through a neural network.
            After checking request validity, passes the request to the nucleus for processing.
            See :obj:`bittensor.proto.ReturnCode` for all possible return codes.
            
            Args:
                request (:obj:`bittensor.proto`, `required`): 
                    Tensor request proto.
                context (:obj:`grpc.ServicerContext`, `required`): 
                    grpc server context.
            
            Returns:
                response (:obj:`bittensor.proto.TensorMessage`): 
                    proto response carring the nucleus backward output or None under failure.
        """
        backward_response_tensors, code, synapses = self._backward( request )
        # TODO(eugene) Shouldnt we be signing these responses ?
        response = bittensor.proto.TensorMessage(
            version = bittensor.__version_as_int__, 
            hotkey = self.wallet.hotkey.ss58_address, 
            return_code = code,
            tensors = backward_response_tensors,
            requires_grad = request.requires_grad,
            synapses = synapses
        )
        return response

    def _forward(self, request):
        r""" Performs validity checks on the grpc request before passing the tensors to the forward queue.
            Returns the outputs and synapses from the backend forward call.
            
            Args:
                request (:obj:`bittensor.proto`, `required`): 
                    Tensor request proto.
            Returns:
                response (:obj:`bittensor.proto.Tensor, `required`): 
                    serialized tensor response from the nucleus call or None.
                code (:obj:`bittensor.proto.ReturnCode`, `required`):
                    Code from the call. This specifies if the overall function call was a success. 
                    This is separate from the synapse returns codes which relate to the individual synapse call. 
                synapses (:obj:`List[ 'bittensor.proto.Synapse' ]` of shape :obj:`(num_synapses)`, `required`):
                    Synapse wire protos with return codes from forward request.
        """
        # ===================================================================
        # ==== First deserialize synapse wire protos to instance objects ====        
        # ===================================================================
        synapses: List['bittensor.Synapse'] = []
        for synapse_wire_proto in request.synapses:
            synapses.append( bittensor.synapse.deserialize( synapse_wire_proto ) )


        # ===================================
        # ==== Init params from synapses ====        
        # ===================================
        # These items are filled through the call and the function returns 
        # when all codes are non-success or the function finishes completely.
        synapse_messages = [ "Success" for _ in synapses ]
        synapse_codes = [ bittensor.proto.ReturnCode.Success for _ in synapses ]
        synapse_inputs = [ None for _ in synapses ]
        synapse_responses = [ None for _ in synapses ] # We fill nones for non success.
        synapse_is_response = [ False for _ in synapses ]
        synapse_call_times = [ 0 for _ in synapses ]
        start_time = clock.time()

        # ==================================================================
        # ==== Function which returns true if all codes are non success ====
        # ==================================================================
        def check_if_should_return() -> bool:
            for code in synapse_codes:
                if code == bittensor.proto.ReturnCode.Success:
                    return False
            return True


        # ==============================================================
        # ==== Function which prints all log statements per synapse ====
        # ==============================================================
        def finalize_codes_stats_and_logs():
            self.stats.forward_elapsed_time.update( clock.time() - start_time )
            for index, _ in enumerate( synapses ):
                self.stats.codes[ synapse_codes[ index ] ] += 1
                request.synapses [ index ].return_code = synapse_codes[ index ] # Set synapse wire proto codes.
                request.synapses [ index ].message = synapse_messages[ index ] # Set synapse wire proto message
                bittensor.logging.rpc_log ( 
                    axon = True, 
                    forward = True, 
                    is_response = synapse_is_response [index], 
                    code = synapse_codes[ index ], 
                    call_time = synapse_call_times[ index ], 
                    pubkey = request.hotkey, 
                    inputs = synapse_inputs [index] , 
                    outputs = None if synapse_codes[ index ] != bittensor.proto.ReturnCode.Success else list( synapse_responses[index].shape ), 
                    message = synapse_messages[ index ]
                )

        # ======================================
        # ==== Check response length ====
        # ======================================
        if len( request.tensors ) != len( synapses ):
            # Not enough responses per request.
            code = bittensor.proto.ReturnCode.ResponseShapeException
            call_time = clock.time() - start_time
            message = "Request length doesn't match synape length."
            synapse_codes = [code for _ in synapses ]
            synapse_call_times = [call_time for _ in synapses ]
            synapse_messages = [ message for _ in synapses ]
            finalize_codes_stats_and_logs()
            return [], bittensor.proto.ReturnCode.ResponseShapeException, request.synapses


        # ===================================
        # ==== Deeerialize/Decode inputs ====
        # ===================================
        deserialized_forward_tensors = []
        for index, synapse in enumerate( synapses ):
            try:
                deserialized_forward_tensors [index] = synapse.deserialize_forward_request_tensor ( request.tensors [index] )
            except Exception as e:
                synapse_codes [index] = bittensor.proto.ReturnCode.RequestSerializationException
                synapse_call_times [index] = clock.time() - start_time
                synapse_messages [index] = 'Input deserialization exception with error:{}'.format(str(e))
        # Check if the call can stop here.
        if check_if_should_return():
            finalize_codes_stats_and_logs()
            return [], bittensor.proto.ReturnCode.RequestSerializationException, request.synapses


        # ===================================
        # ==== Make forward calls. =========
        # ===================================
        try:
            if self.priority != None:
                priority = self.priority( request.hotkey, inputs_x = deserialized_forward_tensors, request_type = bittensor.proto.RequestType.FORWARD )
                future = self.priority_threadpool.submit (
                    self.forward_callback,
                    inputs_x = deserialized_forward_tensors, 
                    synapses = synapses,
                    priority = priority
                )
                forward_response_tensors, codes, messages = future.result( timeout= self.forward_timeout )
            else:
                forward_response_tensors, codes, messages = self.forward_callback(
                    inputs_x = deserialized_forward_tensors,
                    synapses = synapses,
                )
            synapse_is_response = [ True for code in synapse_codes if code == bittensor.proto.ReturnCode.Success  ]

        # ========================================
        # ==== Catch forward request timeouts ====
        # ========================================
        except concurrent.futures.TimeoutError:
            code = bittensor.proto.ReturnCode.Timeout
            call_time = clock.time() - start_time
            message = "Request reached timeout"
            synapse_codes = [code for _ in synapses ]
            synapse_call_times = [call_time for _ in synapses ]
            synapse_messages = [ message for _ in synapses ]
            finalize_codes_stats_and_logs()
            return [], bittensor.proto.ReturnCode.Timeout, request.synapses

        # ==================================
        # ==== Catch unknown exceptions ====
        # ==================================
        except Exception as e:
            code = bittensor.proto.ReturnCode.UnknownException
            call_time = clock.time() - start_time
            message = str ( e )
            synapse_codes = [code for _ in synapses ]
            synapse_call_times = [call_time for _ in synapses ]
            synapse_messages = [ message for _ in synapses ]
            finalize_codes_stats_and_logs()
            return [], bittensor.proto.ReturnCode.UnknownException, request.synapses


        # ====================================
        # ==== Encode/serialize responses ====
        # ====================================
        for index, forward_response_tensor, synapse in enumerate( list(zip(forward_response_tensors, synapses))):
            try:
                synapse_responses [ index ] = synapse.serialize_forward_response_tensor( forward_response_tensor )
            except Exception as e:
                synapse_codes [ index ]= bittensor.proto.ReturnCode.ResponseSerializationException
                synapse_call_times [ index ] = clock.time() - start_time
                synapse_messages [index] = "Synapse response serialization exception with error: {}".format( str( e ) )
        # Check if the call can stop here.
        if check_if_should_return():
            finalize_codes_stats_and_logs()
            return [], bittensor.proto.ReturnCode.ResponseSerializationException, request.synapses


        # =========================================================
        # ==== Set return times for successfull forward ===========
        # =========================================================
        for index, _ in enumerate( synapses ):
            if synapse_codes[index] == bittensor.proto.ReturnCode.Success:
                synapse_call_times[index] = clock.time() - start_time

        finalize_codes_stats_and_logs()
        return synapse_responses, bittensor.proto.ReturnCode.Success, request.synapses
 
    def _backward(self, request):
        r""" Performs validity checks on the grpc request before piping the request to backend queue.
            Returns the outputs and synapses (with codes and messages from the backward call.)
            Args:
                request (:obj:`bittensor.proto`, `required`): 
                    Tensor request proto.
            Returns:
                response: (:obj:`bittensor.proto.Tensor, `required`): 
                    serialized tensor gradient responses. This is always an empty vector until gradients are allowed.
                code (:obj:`bittensor.proto.ReturnCode`, `required`):
                    Code from the call. This specifies if the overall function call was a success. 
                    This is separate from the synapse returns codes which relate to the individual synapse call. 
                synapses (:obj:`List[ 'bittensor.proto.Synapse' ]` of shape :obj:`(num_synapses)`, `required`):
                    Synapse wire protos with return codes from forward request.
        """

        # ===================================================================
        # ==== First deserialize synapse wire protos to instance objects ====        
        # ===================================================================
        synapses: List['bittensor.Synapse'] = []
        for synapse_wire_proto in request.synapses:
            synapses.append( bittensor.synapse.deserialize( synapse_wire_proto ) )


        # ===================================
        # ==== Init params from synapses ====        
        # ===================================
        # These items are filled through the call and the function returns 
        # when all codes are non-success or the function finishes completely.
        synapse_messages = [ "Success" for _ in synapses ]
        synapse_codes = [ bittensor.proto.ReturnCode.Success for _ in synapses ]
        deserialized_forward_tensors = [ None for _ in synapses ]
        deserialized_forward_gradients = [ None for _ in synapses ]
        synapse_is_response = [ False for _ in synapses ]
        synapse_call_times = [ 0 for _ in synapses ]
        start_time = clock.time()

        # ==================================================================
        # ==== Function which returns true if all codes are non success ====
        # ==================================================================
        def check_if_should_return() -> bool:
            for code in synapse_codes:
                if code == bittensor.proto.ReturnCode.Success:
                    return False
            return True


        # ==============================================================
        # ==== Function which prints all log statements per synapse ====
        # ==============================================================
        def finalize_codes_stats_and_logs():
            self.stats.forward_elapsed_time.update( clock.time() - start_time )
            for index, _ in enumerate( synapses ):
                self.stats.codes[ synapse_codes[ index ] ] += 1
                request.synapses [ index ].return_code = synapse_codes[ index ] # Set synapse wire proto codes.
                request.synapses [ index ].message = synapse_messages[ index ] # Set synapse wire proto message
                bittensor.logging.rpc_log ( 
                    axon = True, 
                    forward = False, 
                    is_response = synapse_is_response [index], 
                    code = synapse_codes[ index ], 
                    call_time = synapse_call_times[ index ], 
                    pubkey = request.hotkey, 
                    inputs = deserialized_forward_gradients [index] , 
                    outputs = None, # we never return from backward. 
                    message = synapse_messages[ index ]
                )


        # ======================================
        # ==== Check request length ====
        # ======================================
        if len( request.tensors ) != 2 * len( synapses ): # 2 per input.
            # Not enough responses per request.
            code = bittensor.proto.ReturnCode.ResponseShapeException
            call_time = clock.time() - start_time
            message = "Request length doesn't match synape length."
            synapse_codes = [code for _ in synapses ]
            synapse_call_times = [call_time for _ in synapses ]
            synapse_messages = [ message for _ in synapses ]
            finalize_codes_stats_and_logs()
            return [], bittensor.proto.ReturnCode.ResponseShapeException, request.synapses


        # ===================================
        # ==== Deeerialize/Decode inputs ====
        # ===================================
        for index, synapse in enumerate( synapses ):
            try:
                deserialized_forward_tensors [index] = synapse.deserialize_forward_request_tensor ( request.tensors [index] )
                deserialized_forward_gradients [index] = synapse.serialize_backward_request_gradient ( request.tensors [ len( synapses ) + index ] )
            except Exception as e:
                synapse_codes [index] = bittensor.proto.ReturnCode.RequestSerializationException
                synapse_call_times [index] = clock.time() - start_time
                synapse_messages [index] = 'Input deserialization exception with error:{}'.format(str(e))
        # Check if the call can stop here.
        if check_if_should_return():
            finalize_codes_stats_and_logs()
            return [], bittensor.proto.ReturnCode.RequestSerializationException, request.synapses


        # ===================================
        # ==== Make backward calls. =========
        # ===================================
        try:
            if self.priority != None:
                # No wait on backward calls.
                priority = self.priority( request.hotkey, inputs_x = deserialized_forward_tensors, request_type = bittensor.proto.RequestType.BACKWARD )
                self.priority_threadpool.submit(
                    self.backward_callback, 
                    inputs_x = deserialized_forward_tensors, 
                    grads_dy = deserialized_forward_gradients,
                    synapses = synapses,
                    priority = priority
                )
            else:
                # Calling default
                # TODO(eugene): does this create a waiting operation.
                self.backward_callback ( deserialized_forward_tensors, deserialized_forward_gradients, synapses = synapses )

        # ==================================
        # ==== Catch unknown exceptions ====
        # ==================================
        except Exception as e:
            code = bittensor.proto.ReturnCode.UnknownException
            call_time = clock.time() - start_time
            message = str ( e )
            synapse_codes = [code for _ in synapses ]
            synapse_call_times = [call_time for _ in synapses ]
            synapse_messages = [ message for _ in synapses ]
            finalize_codes_stats_and_logs()
            return [], bittensor.proto.ReturnCode.UnknownException, request.synapses

        # ==============================
        # ==== Finalize call times =====
        # ==============================
        for index, _ in enumerate( synapses ):
            if synapse_codes[index] == bittensor.proto.ReturnCode.Success:
                synapse_call_times[index] = clock.time() - start_time

        finalize_codes_stats_and_logs()
        return [], bittensor.proto.ReturnCode.Success, request.synapses



    def default_forward_callback(self, inputs_x:torch.FloatTensor, synapses=[] ):
        """
            The default forward callback when no callback is attached: Is used to call specific synapse functions

            Args:
                inputs_x (:obj:`torch.FloatTensor`, `required`): 
                    The inputs that will be passed to the synapse functions
                
                synapses (:obj: list of bittensor.proto.SynapseArgs, 'Optional')
                    The proto message that contains additional args for individual synapse functions

            Returns:
                response_tensors: (:obj: list of bittensor.proto.Tensor, `required`): 
                    serialized tensor response from the nucleus call or None.
                response_codes: (:obj: list of bittensor.proto.ReturnCode, `required`)
                    return code associated with forward call i.e. Success of Timeout.
                response_messages: (:obj: list of strings, `required`)
                    return message associated with synapse call
        """
        # --- initialize response variables --- 
        response_tensors = []
        response_codes = []
        response_messages = []
        
        # --- calling attached synapses ---
        for synapse in synapses:
            try:
                if synapse.synapse_type in self.synapse_callbacks and self.synapse_callbacks[synapse.synapse_type] != None:
                    response_tensors.append(self.synapse_callbacks[synapse.synapse_type](inputs_x, synapse))
                    response_codes.append(bittensor.proto.ReturnCode.Success)
                    response_messages.append('Success')
                else:
                    response_tensors.append(None)
                    response_codes.append(bittensor.proto.ReturnCode.NotImplemented)
                    response_messages.append('Not Implemented')

            except Exception as e:
                # --- Exception Hit in Synapse ---
                response_tensors.append(None)
                response_codes.append(bittensor.proto.ReturnCode.UnknownException)
                response_messages.append(str(e))
        return response_tensors, response_codes, response_messages

    def default_backward_callback(self, inputs_x:torch.FloatTensor, grads_dy:torch.FloatTensor, synapses=[] ):
        """
            The default forward callback when no callback is attached: Is used to call specific synapse functions

            Args:
                inputs_x (:obj:`torch.FloatTensor`, `required`): 
                    The inputs that will be passed to the synapse functions
                
                synapses (:obj: list of bittensor.proto.SynapseArgs, 'Optional')
                    The proto message that contains additional args for individual synapse functions

            Returns:
                response_tensors: (:obj: list of bittensor.proto.Tensor, `required`): 
                    serialized tensor response from the nucleus call or None.
                response_codes: (:obj: list of bittensor.proto.ReturnCode, `required`)
                    return code associated with forward call i.e. Success of Timeout.
                response_messages: (:obj: list of strings, `required`)
                    return message associated with synapse call
        """
        # --- initialize response variables --- 
        response_tensors = []
        response_codes = []
        response_messages = []
        
        # --- calling attached synapses ---
        with torch.enable_grad() and torch.autograd.set_detect_anomaly(True):
            for index, synapse in enumerate(synapses):
                try:
                    if synapse.synapse_type in self.synapse_callbacks and self.synapse_callbacks[synapse.synapse_type] != None:
                        response_tensor = self.synapse_callbacks[synapse.synapse_type](inputs_x, synapse)
                        torch.autograd.backward (
                            tensors = [ response_tensor ],
                            grad_tensors = [ grads_dy[index] ],
                            retain_graph=True
                        )                        

                        response_tensors.append(None)
                        response_codes.append(bittensor.proto.ReturnCode.Success)
                        response_messages.append('Success')
                    else:
                        response_tensors.append(None)
                        response_codes.append(bittensor.proto.ReturnCode.NotImplemented)
                        response_messages.append('Not Implemented')

                except Exception as e:
                    # --- Exception Hit in Synapse ---
                    response_tensors.append(None)
                    response_codes.append(bittensor.proto.ReturnCode.UnknownException)
                    response_messages.append(str(e))

        self.optimizer.step()
        self.optimizer.zero_grad()
        
        return response_tensors, response_codes, response_messages

    def attach_forward_callback(self, forward_callback: Callable[ [str, torch.Tensor, int], torch.Tensor ]):
        """ Assigns the forward_callback.

            Returns:
                forward_callback (:callabl:`Callable[ [str, torch.Tensor, int], torch.Tensor `, `required`): 
                    Forward function called on recieving a forward request.
        """
        bittensor.axon.check_forward_callback(forward_callback)
        self.forward_callback = forward_callback

    def attach_synapse_callback(self, synapse_callback: Callable[[str, torch.Tensor, int],torch.Tensor], synapse_type ):
        """ Assigns the callback to a specific synapse.

            Args:
                synapse_callback (:callabl:`Callable[ [str, torch.Tensor, int], torch.Tensor `, `required`): 
                    function called for a specific synapse.
        """
        self.synapse_callbacks[synapse_type] = synapse_callback

    def attach_backward_callback(self, backward_callback: Callable[ [str, torch.Tensor, torch.Tensor, int], torch.Tensor ], modality: int ):
        """ Assigns the backward_callback call to this neuron.

            Returns:
                backward_callback (:callabl:`Callable[ [torch.Tensor, torch.Tensor], torch.Tensor `, `required`): 
                     Backward callback called on recieving a backward request.
        """
        bittensor.axon.check_backward_callback(backward_callback)
        self.backward_callback = backward_callback

    def __del__(self):
        r""" Called when this axon is deleted, ensures background threads shut down properly.
        """
        self.stop()

    def serve( 
            self, 
            use_upnpc: bool = False, 
            subtensor: 'bittensor.Subtensor' = None,
            network: str = None,
            chain_endpoint: str = None,
            prompt: bool = False
        ) -> 'Axon':
        r""" Subscribes this Axon servicing endpoint to the passed network using it's wallet.
            Args:
                use_upnpc (:type:bool, `optional`): 
                    If true, serves the axon attempts port forward through your router before 
                    subscribing.
                subtensor (:obj:`bittensor.Subtensor`, `optional`): 
                    Chain connection through which to serve.
                network (default='local', type=str)
                    If subtensor is not set, uses this network flag to create the subtensor connection.
                chain_endpoint (default=None, type=str)
                    Overrides the network argument if not set.
                prompt (bool):
                    If true, the call waits for confirmation from the user before proceeding.

        """   
        if subtensor == None: subtensor = bittensor.subtensor( network = network, chain_endpoint = chain_endpoint) 
        serv_success = subtensor.serve_axon( axon = self, use_upnpc = use_upnpc, prompt = prompt )
        if not serv_success:
            raise RuntimeError('Failed to serve neuron.')
        return self


    def start(self) -> 'Axon':
        r""" Starts the standalone axon GRPC server thread.
        """
        if self.server != None:
            self.server.stop( grace = 1 )  
            logger.success("Axon Stopped:".ljust(20) + "<blue>{}</blue>", self.ip + ':' + str(self.port))

        self.server.start()
        logger.success("Axon Started:".ljust(20) + "<blue>{}</blue>", self.ip + ':' + str(self.port))
        self.started = True
        return self

    def stop(self) -> 'Axon':
        r""" Stop the axon grpc server.
        """
        if self.server != None:
            self.server.stop( grace = 1 )
            logger.success("Axon Stopped:".ljust(20) + "<blue>{}</blue>", self.ip + ':' + str(self.port))
        self.started = False
        return self
    
    def check(self):
        r""" Checks axon's forward and backward callbacks 
        """
        pubkey = self.wallet.hotkey.ss58_address
        if self.forward_callback != None:
            bittensor.axon.check_forward_callback(self.forward_callback,index,pubkey)

        if self.backward_callback != None:
            bittensor.axon.check_backward_callback(backward,index,pubkey)
        return self

    def _init_stats(self):
        return SimpleNamespace(
            # Queries per second.
            qps = stat_utils.EventsPerSecondRollingAverage( 0, 0.01 ),
            # Total requests.
            total_requests = 0,
            # Total bytes recieved per second.
            total_in_bytes = 0,
            # Total bytes responded per second.
            total_out_bytes = 0,
            # Bytes recieved per second.
            avg_in_bytes_per_second = stat_utils.AmountPerSecondRollingAverage( 0, 0.01 ),
            # Bytes responded per second.
            avg_out_bytes_per_second = stat_utils.AmountPerSecondRollingAverage( 0, 0.01 ),
            # Requests per pubkey.
            requests_per_pubkey = {},
            # Success per pubkey.
            successes_per_pubkey = {},
            # Query time per pubkey.
            query_times_per_pubkey = {},
            # Queries per second per pubkey.
            qps_per_pubkey = {},
            # Codes recieved per pubkey.
            codes_per_pubkey = {},
            # Bytes recieved per pubkey.
            avg_in_bytes_per_pubkey = {},
            # Bytes sent per pubkey.
            avg_out_bytes_per_pubkey = {}
        )

    def update_stats_for_request(self, request, response, time, code):
        r""" Updates statistics for this request and response.
            Args:
                requests ( bittensor.proto.TensorMessage, `required`):
                    The request.
                response ( bittensor.proto.TensorMessage, `required`):
                    The response.
                time (:type:`float`, `required`):
                    Length of call in seconds.
                code (:obj:`bittensor.proto.ReturnCode, `required`)
                    Return code associated with the call i.e. Success of Timeout.
        """
        self.stats.qps.event()
        self.stats.total_requests += 1
        self.stats.total_in_bytes += sys.getsizeof(request) 
        self.stats.total_out_bytes += sys.getsizeof(response) 
        self.stats.avg_in_bytes_per_second.event( float(sys.getsizeof(request)) )
        self.stats.avg_out_bytes_per_second.event( float(sys.getsizeof(response)) )
        pubkey = request.hotkey
        if pubkey not in self.stats.requests_per_pubkey:
            self.stats.requests_per_pubkey[ pubkey ] = 0
            self.stats.successes_per_pubkey[ pubkey ] = 0
            self.stats.query_times_per_pubkey[ pubkey ] = stat_utils.AmountPerSecondRollingAverage(0, 0.05)
            self.stats.qps_per_pubkey[ pubkey ] = stat_utils.EventsPerSecondRollingAverage(0, 0.05)
            self.stats.codes_per_pubkey[ pubkey ] = dict([(k,0) for k in bittensor.proto.ReturnCode.keys()])
            self.stats.avg_in_bytes_per_pubkey[ pubkey ] = stat_utils.AmountPerSecondRollingAverage(0, 0.01)
            self.stats.avg_out_bytes_per_pubkey[ pubkey ] = stat_utils.AmountPerSecondRollingAverage(0, 0.01)

        # Add values.
        self.stats.requests_per_pubkey[ pubkey ] += 1
        self.stats.successes_per_pubkey[ pubkey ] += 1 if code == 1 else 0
        self.stats.query_times_per_pubkey[ pubkey ].event( float(time) )
        self.stats.avg_in_bytes_per_pubkey[ pubkey ].event( float(sys.getsizeof(request)) )
        self.stats.avg_out_bytes_per_pubkey[ pubkey ].event( float(sys.getsizeof(response)) )
        self.stats.qps_per_pubkey[ pubkey ].event()    
        try:
            if bittensor.proto.ReturnCode.Name( code ) in self.stats.codes_per_pubkey[ pubkey ].keys():
                self.stats.codes_per_pubkey[ pubkey ][bittensor.proto.ReturnCode.Name( code )] += 1
        except:
            pass  

    def to_dataframe ( self, metagraph ):
        r""" Return a stats info as a pandas dataframe indexed by the metagraph or pubkey if not existend.
            Args:
                metagraph: (bittensor.Metagraph):
                    Indexes the stats data using uids.
            Return:
                dataframe (:obj:`pandas.Dataframe`)
        """
        # Reindex the pubkey to uid if metagraph is present.
        try:
            index = [ metagraph.hotkeys.index(pubkey) for pubkey in self.stats.requests_per_pubkey.keys() if pubkey in metagraph.hotkeys ]
            columns = [ 'axon_n_requested', 'axon_n_success', 'axon_query_time','axon_avg_inbytes','axon_avg_outbytes', 'axon_qps' ]
            dataframe = pandas.DataFrame(columns = columns, index = index)
            for pubkey in self.stats.requests_per_pubkey.keys():
                if pubkey in metagraph.hotkeys:
                    uid = metagraph.hotkeys.index(pubkey)
                    dataframe.loc[ uid ] = pandas.Series( {
                        'axon_n_requested': int(self.stats.requests_per_pubkey[pubkey]),
                        'axon_n_success': int(self.stats.requests_per_pubkey[pubkey]),
                        'axon_query_time': float(self.stats.query_times_per_pubkey[pubkey].get()),             
                        'axon_avg_inbytes': float(self.stats.avg_in_bytes_per_pubkey[pubkey].get()),
                        'axon_avg_outbytes': float(self.stats.avg_out_bytes_per_pubkey[pubkey].get()),
                        'axon_qps': float(self.stats.qps_per_pubkey[pubkey].get())
                    } )
            dataframe['uid'] = dataframe.index
            return dataframe

        except Exception as e:
            bittensor.logging.error(prefix='failed axon.to_dataframe()', sufix=str(e))
            return pandas.DataFrame()

    def to_wandb( self ):
        r""" Return a dictionary of axon stat info for wandb logging
            Args:
                metagraph: (bittensor.Metagraph):
                    If not None, indexes the wandb data using int uids rather than string pubkeys.
            Return:
                wandb_info (:obj:`Dict`)
        """
        try:
            avg_query_time = 0.0
            for pubkey in self.stats.query_times_per_pubkey:
                avg_query_time += self.stats.query_times_per_pubkey[pubkey].get() / len( self.stats.query_times_per_pubkey )
            # ---- Axon summary for wandb
            wandb_data = {
                'axon/qps': self.stats.qps.get(),
                'axon/avg_query_time': avg_query_time,
                'axon/total_requests': self.stats.total_requests,
                'axon/total_in_bytes' : self.stats.total_in_bytes,
                'axon/total_out_bytes' : self.stats.total_out_bytes,
                'axon/avg_in_bytes_per_second' : self.stats.avg_in_bytes_per_second.get(),
                'axon/avg_out_bytes_per_second' : self.stats.avg_out_bytes_per_second.get(),
            }
            return wandb_data
        except Exception as e:
            bittensor.logging.error(prefix='failed during axon.to_wandb()', sufix=str(e))
            return {} 