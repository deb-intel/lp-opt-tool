#
#  -*- coding: utf-8 -*-
#

import os
import re
import logging
from collections import namedtuple
import tensorflow as tf

from google.protobuf import text_format
from tensorflow.core.framework import graph_pb2
from tensorflow.core.framework import attr_value_pb2
from tensorflow.core.framework import node_def_pb2
from tensorflow.python.platform import gfile
from tensorflow.python.framework import graph_util
from tensorflow.python.saved_model import signature_constants
from tensorflow.python.saved_model import tag_constants
from tensorflow.python.framework.ops import Graph
from tensorflow.python.framework.convert_to_constants import convert_variables_to_constants_v2
from tensorflow.python.framework import tensor_shape
from tensorflow.python.framework import tensor_util

from ilit.utils.utility import singleton


@singleton
class TFGraphAnalyzer(object):
    """Tensorflow Graph Analyzer class which implemented under singleton mode.
    This class provides the following API:
    * Analyze the graph
    * Analyze the input/output node names of the specified graph
    """
    # TODO add the positive input flag
    node_details = namedtuple('node_details', ['node', 'outputs'])

    def __init__(self):
        self.logger = logging.getLogger()
        self._graph = None

    @property
    def graph(self):
        return self._graph

    @graph.setter
    def graph(self, new_graph):
        self._graph = new_graph

    def _has_positive_input(self, start_node):
        op_type = start_node.op
        if op_type in ("Relu", "Relu6") or op_type.find("AndRelu") != -1:
            return True
        elif op_type.startswith("Quantized") and not op_type.endswith("AndRelu"):
            return False
        elif op_type in ("Concat", "Add", "AddV2", "AddN"):
            for each_input in start_node.input:
                has_relu = self._has_positive_input(self.node_name_details[each_input].node)
                if not has_relu:
                    return False
            return True
        elif op_type in ("Conv2D", "DepthwiseConv2D", "QuantizeV2", "DepthwiseConv2dNative",
                         "MaxPool", "Requantize", "AvgPool", "Pad", "CropAndResize", "Dequantize",
                         "Mean", "MatMul"):
            return self._has_positive_input(
                self.node_name_details[TFGraphRewriterHelper.node_name_from_input(
                    start_node.input[0])])
        else:
            return False

    def get_graph_input_output(self):
        input_node_names = []
        output_node_names = []
        for _, i in self.node_name_details.items():
            if i.node.op == 'Const':
                continue
            if not i.node.input and not i.outputs:
                self.logger.debug("skip isolated node .. {}".format(i.node.name))
            elif not i.node.input:
                input_node_names.append(i.node.name)
            elif not i.outputs:
                output_node_names.append(i.node.name)
            else:
                pass

        self.logger.debug("Found possible input node names: {}, output node names: {}".format(
            input_node_names, output_node_names))

        return (input_node_names, output_node_names)

    def search_patterns(self, input_pattern):
        """search user specified patterns on internal grpah structure.

        Args:
            input_pattern (list): The element of the pattern list could be string/list/tuple.
            string or list means the specified types are mandatory while tuple stands for optional.
            e.g:
            ['Conv2D', ['BiasAdd'], ("Add", "AddN"), ["Relu","Relu6"]] it equals to below patterns:
            Conv2D + BiasAdd + Add + Relu
            Conv2D + BiasAdd + AddN + Relu
            Conv2D + BiasAdd + Add + Relu6
            Conv2D + BiasAdd + AddN + Relu6
            Conv2D + BiasAdd + Relu
            Conv2D + BiasAdd + Relu6

        Return: list. Each matched pattern composed of matched node name and we put the match node
                    op as the last element of each pair.
                    e.g
                    [
                        ['resnet_model/conv2d_4/Conv2D',
                        'resnet_model/batch_normalization_4/FusedBatchNorm',
                        'resnet_model/add',
                        'resnet_model/Relu_3',
                        ['Conv2D', 'BiasAdd', 'Add', 'Relu']],
                        ['resnet_model/conv2d_7/Conv2D',
                        'resnet_model/batch_normalization_7/FusedBatchNorm',
                        'resnet_model/add_1',
                        'resnet_model/Relu_6',
                        ['Conv2D', 'BiasAdd', 'AddN', 'Relu6']]
                    ]
        """
        def validate_input(data, creteria):
            if isinstance(creteria, str) and data == creteria:
                return True
            elif isinstance(creteria, (list, tuple)) and data in creteria:
                return True
            else:
                return False

        output_result = []
        minimal_match_count = len([i for i in input_pattern if isinstance(i, (str, list))])
        for _, v in self.node_name_details.items():
            start_index = len(input_pattern) - 1
            while start_index >= 0:
                find_first_match = validate_input(v.node.op, input_pattern[start_index])
                if find_first_match:
                    break
                elif isinstance(input_pattern[start_index], tuple):
                    start_index -= 1
                    continue
                else:
                    start_index = -2

            if start_index < 0:
                continue

            pattern_index = start_index - 1
            single_set_res = []
            matched_op_type = []
            cur_node = v.node
            continue_search_flag = True
            single_set_res.append(cur_node.name)
            matched_op_type.append(cur_node.op)
            while continue_search_flag and pattern_index >= 0:
                cur_node_name = TFGraphRewriterHelper.node_name_from_input(cur_node.input[0])
                if validate_input(self.node_name_details[cur_node_name].node.op,
                                  input_pattern[pattern_index]):
                    pattern_index -= 1
                    cur_node = self.node_name_details[cur_node_name].node
                    single_set_res.append(cur_node.name)
                    matched_op_type.append(cur_node.op)
                elif isinstance(input_pattern[pattern_index], tuple):
                    pattern_index -= 1
                    continue_search_flag = True
                    continue
                else:
                    continue_search_flag = False

            if len(matched_op_type) >= minimal_match_count:
                single_set_res.reverse()
                matched_op_type.reverse()
                single_set_res.append(matched_op_type)
                output_result.append(single_set_res)

        longest_match = {}
        final_output = []
        for i in output_result:
            key = i[0]
            if key not in longest_match:
                longest_match[key] = i[-1]

            if len(longest_match[key]) < len(i[-1]):
                longest_match[key] = i[-1]

        for i in output_result:
            if i[0] in longest_match and i[-1] == longest_match[i[0]]:
                final_output.append(i)

        return final_output

    def remove_node_with_single_input_output(self, node_name):
        """Remove node with one input and rebuild internal graph data structure.

        Args:
            node_name (string): node name

        Returns:
            [bool]: True if remove the node without exception,
                    False if failed to remove it.
        """
        if node_name not in self.node_name_details:
            self.logger.debug("The {} is not a valid node name".format(node_name))
            return False

        non_const_node_count = len([
            TFGraphRewriterHelper.node_name_from_input(i)
            for i in self.node_name_details[node_name].node.input if self.node_name_details[
                TFGraphRewriterHelper.node_name_from_input(i)].node.op != "Const"
        ])

        if non_const_node_count > 1:
            self.logger.debug("The target node {} has more than one input.".format(node_name))
            return False

        try:

            top_node_name = TFGraphRewriterHelper.node_name_from_input(
                self.node_name_details[node_name].node.input[0])

            for bottom_node_name in self.node_name_details[node_name].outputs:
                update_output_name = [
                    bottom_node_name if i == node_name else i
                    for i in self.node_name_details[top_node_name].outputs
                ]
                self.node_name_details[top_node_name]._replace(outputs=update_output_name)

                update_input_name = [
                    self.node_name_details[node_name].node.input[0] if i == node_name else i
                    for i in self.node_name_details[bottom_node_name].node.input
                ]

                if self.node_name_details[bottom_node_name].node.input:
                    self.node_name_details[bottom_node_name].node.ClearField('input')
                    self.node_name_details[bottom_node_name].node.input.extend(update_input_name)

        except Exception as e:
            self.logger.debug("Failed to remove node {} due to {}".format(node_name, str(e)))
            return False
        else:
            return self.remove_node(node_name)

    def remove_node(self, node_name):
        """Remove the user specified node by its name.

        Args:
            node_name (string): node name string.

        Returns:
            [bool]: True if remove the node without exception.
                    False if failed to remove it.
        """

        if node_name not in self.node_name_details:
            self.logger.debug("The {} is not a valid node name".format(node_name))
            return False
        try:
            self.node_name_details.pop(node_name)
        except Exception as e:
            self.logger.info("Failed to remove {} due to {}".format(node_name, str(e)))
            return False
        else:
            self.logger.debug("{} has been removed.".format(node_name))
            return True

    def replace_const_node(self, new_const_node, target_node, old_constant_node_name):
        """Replace the specified const node with another one.

        Args:
            new_const_node (NodeDef): node name string.
            target_node (list): the string list that contains name of node that
                                need to be replaced const node.
            old_constant_node_name (string): the outdated const node name.

        """
        new_const_node_name = new_const_node.name

        self.node_name_details[new_const_node_name] = self.node_details(node=new_const_node,
                                                                        outputs=target_node)

        for sub_node in target_node:
            for index, each_node_name in enumerate(self.node_name_details[sub_node].node.input):
                if each_node_name == old_constant_node_name:
                    new_input_name = self.node_name_details[sub_node].node.input[:index] + [
                        new_const_node_name
                    ] + self.node_name_details[sub_node].node.input[index + 1:]
                    self.node_name_details[sub_node].node.ClearField('input')
                    self.node_name_details[sub_node].node.input.extend(new_input_name)

    def replace_constant_graph_with_constant_node(self, new_node, old_end_node_name,
                                                  output_node_name):
        """remove sub-graph with a const node

        Args:
            new_node (nodedef): the constant node 
            old_end_node_name (string):  the sub-graph end node which will be updated by new node

        Returns:
            [bool]: True if remove the node without exception.
                    False if failed to remove it.
        """
        new_node_name = new_node.name

        if new_node.op != "Const":
            self.logger.debug("input of replace_with_constant_node must be a constant node")
            return False
        try:
            inputs = self.node_name_details[old_end_node_name].node.input
            inputs = [TFGraphRewriterHelper.node_name_from_input(i) for i in inputs]
            for input_name in inputs:
                if self.node_name_details[input_name].node.op != "Const":
                    self.logger.debug("the subgraph replaces must be constant")
                    return False
                else:
                    self.node_name_details.pop(input_name)
            output_node_name = self.node_name_details[old_end_node_name].outputs
            self.replace_node(new_node, old_end_node_name, output_node_name)
            self.node_name_details[new_node_name].node.ClearField('input')
        except Exception as e:
            self.logger.info("Failed to replace {} due to {}".format(old_end_node_name, str(e)))
            return False
        else:
            self.logger.debug("{} has been replaced.".format(old_end_node_name))
            return True

    def replace_node(self, new_node, old_node_name, output_nodes_name):
        """Replace the node into the internal data structure node_name_details

        Args:
            new_node (nodedef): the nodedef object.
            old_node_name (string): the parent node of input node.
            output_nodes_name (string list): output node names list
        """
        node_name = new_node.name
        self.node_name_details[node_name] = self.node_details(node=new_node,
                                                              outputs=output_nodes_name)
        old_node = self.node_name_details[old_node_name].node
        input_nodes_name = [TFGraphRewriterHelper.node_name_from_input(i) for i in old_node.input]
        for input_node_name in input_nodes_name:
            if input_node_name in self.node_name_details:
                self.node_name_details[input_node_name].outputs.remove(old_node_name)
                self.node_name_details[input_node_name].outputs.append(node_name)

        for node_name in output_nodes_name:
            for index, each_node_name in enumerate(self.node_name_details[node_name].node.input):
                if self.node_name_details[
                        node_name].node.input and TFGraphRewriterHelper.node_name_from_input(
                            each_node_name) == old_node_name:
                    new_input_name = self.node_name_details[node_name].node.input[:index] + [
                        node_name
                    ] + self.node_name_details[node_name].node.input[index + 1:]
                    self.node_name_details[node_name].node.ClearField('input')
                    self.node_name_details[node_name].node.input.extend(new_input_name)
        self.remove_node(old_node_name)

    def add_node(self, input_node, start_node_name, end_node_names):
        """Add the node into the internal data structure node_name_details

        Args:
            input_node (nodedef): the nodedef object.
            start_node_name (string): the parent node of input node.
            end_node_names (string list): output node names list
        """
        node_name = input_node.name

        if node_name in self.node_name_details:
            self.logger.debug("Remove the existed node {} from internal data structure".format(
                (node_name)))
            self.node_name_details.pop(node_name)

        self.node_name_details[node_name] = self.node_details(node=input_node,
                                                              outputs=end_node_names)

        for end_node_name in end_node_names:
            # Update start node's output info
            if start_node_name:
                self.node_name_details[start_node_name].outputs.remove(end_node_name)

            # reset output node's input
            for index, each_node_name in enumerate(
                    self.node_name_details[end_node_name].node.input):
                if self.node_name_details[
                        end_node_name].node.input and TFGraphRewriterHelper.node_name_from_input(
                            each_node_name) == start_node_name:
                    new_input_name = self.node_name_details[end_node_name].node.input[:index] + [
                        node_name
                    ] + self.node_name_details[end_node_name].node.input[index + 1:]
                    self.node_name_details[end_node_name].node.ClearField('input')
                    self.node_name_details[end_node_name].node.input.extend(new_input_name)

        # add the inserted node into the start node's output.
        if start_node_name:
            self.node_name_details[start_node_name].outputs.append(node_name)

    def dump_graph(self):
        """Dump the current model's graphdef

        Returns:
            [graphdef]: A graphdef object
        """
        output_graph_def = graph_pb2.GraphDef()
        for _, v in self.node_name_details.items():
            output_graph_def.node.extend([v.node])

        return output_graph_def

    def parse_graph(self, input_graph_def=None):
        """Analyze the input graphdef and return the list contains each node's input/output
            node names

        Args:
            input_graph_def ([graphdef]): graphdef object

        Returns:
            [list]: A list contains each node's inputs/outputs info.
        """

        if not input_graph_def:
            input_graph_def = self._graph

        self.node_name_details = {}

        for node in input_graph_def.node:
            node_name = TFGraphRewriterHelper.node_name_from_input(node.name)

            each_node = self.node_details(node=node, outputs=[])

            if node_name not in self.node_name_details:
                self.node_name_details[node_name] = each_node

        for node_name, node_details in self.node_name_details.items():
            # update the upper node's output infomation.
            for each_input in node_details.node.input:
                self.node_name_details[TFGraphRewriterHelper.node_name_from_input(
                    each_input)].outputs.append(node_name)

        return self.node_name_details


class TFGraphRewriterHelper(object):
    node_name_cache = {}
    node_name_port_cache = {}

    @staticmethod
    def compare_node_attr(node_a, node_b):
        """Compare two node has identical attributes or not.

        Args:
            node_a (nodedef): Input node.
            node_b (nodedef): Another node to be compared.

        Returns:
            [bool]: True if two node have the identical attributes.
        """
        if len(node_a.input) > 1:
            return False

        if node_a.input != node_b.input:
            return False

        if node_a.op != node_b.op:
            return False

        if len(node_a.attr) != len(node_b.attr):
            return False

        node_a_attr = sorted(list(node_a.attr))
        node_b_attr = sorted(list(node_b.attr))

        if node_a_attr != node_b_attr:
            return False

        for attr_name in node_a_attr:
            if node_a.attr[attr_name] != node_b.attr[attr_name]:
                return False

        return True

    @staticmethod
    def create_node(op, name, inputs):
        new_node = node_def_pb2.NodeDef()
        new_node.op = op
        new_node.name = name
        for input_name in inputs:
            new_node.input.extend([input_name])
        return new_node

    @staticmethod
    def create_constant_node(name, value, dtype, shape=None, device='cpu'):
        node = TFGraphRewriterHelper.create_node("Const" if device == 'cpu' else "HostConst", name,
                                                 [])
        TFGraphRewriterHelper.set_attr_dtype(node, "dtype", dtype)
        TFGraphRewriterHelper.set_attr_tensor(node, "value", value, dtype, shape)
        return node

    @staticmethod
    def copy_attr(node, key, attr_value):
        node.attr[key].CopyFrom(attr_value)

    @staticmethod
    def set_attr_dtype(node, key, value):
        node.attr[key].CopyFrom(attr_value_pb2.AttrValue(type=value.as_datatype_enum))

    @staticmethod
    def set_attr_shape(node, key, value):
        node.attr[key].CopyFrom(
            attr_value_pb2.AttrValue(shape=tensor_shape.as_shape(value).as_proto()))

    @staticmethod
    def set_attr_tensor(node, key, value, dtype, shape=None):
        node.attr[key].CopyFrom(
            attr_value_pb2.AttrValue(
                tensor=tensor_util.make_tensor_proto(value, dtype=dtype, shape=shape)))

    @staticmethod
    def set_attr_string(node, key, value):
        node.attr[key].CopyFrom(attr_value_pb2.AttrValue(s=value))

    @staticmethod
    def set_attr_int_list(node, key, value):
        list_value = attr_value_pb2.AttrValue.ListValue(i=value)
        node.attr[key].CopyFrom(attr_value_pb2.AttrValue(list=list_value))

    @staticmethod
    def set_attr_bool(node, key, value):
        node.attr[key].CopyFrom(attr_value_pb2.AttrValue(b=value))

    @staticmethod
    def set_attr_int(node, key, value):
        node.attr[key].CopyFrom(attr_value_pb2.AttrValue(i=value))

    @staticmethod
    def set_attr_float(node, key, value):
        node.attr[key].CopyFrom(attr_value_pb2.AttrValue(f=value))

    @staticmethod
    def ensure_tensor_name_has_port(node_name):
        """Makes sure that a tensor name has :0 if no explicit port exists."""
        if node_name not in TFGraphRewriterHelper.node_name_port_cache:
            key = node_name
            m = re.search(r"(.*):\d+$", node_name)
            if not m:
                node_name = node_name + ":0"
            TFGraphRewriterHelper.node_name_port_cache[key] = node_name
            return node_name
        else:
            return TFGraphRewriterHelper.node_name_port_cache[node_name]

    @staticmethod
    def node_name_from_input(node_name):
        if node_name not in TFGraphRewriterHelper.node_name_cache:
            key = node_name
            if node_name.startswith("^"):
                node_name = node_name[1:]
            m = re.search(r"(.*):\d+$", node_name)
            if m:
                node_name = m.group(1)
            TFGraphRewriterHelper.node_name_cache[key] = node_name
            return node_name
        else:
            return TFGraphRewriterHelper.node_name_cache[node_name]

    @staticmethod
    def unique_node_name_from_input(node_name):
        return node_name.replace(":", "__port__").replace("^", "__hat__")

    @staticmethod
    def values_from_const(node_def):
        """Extracts the values from a const NodeDef as a numpy ndarray.

        Args:
          node_def: Const NodeDef that has the values we want to access.

        Returns:
          Numpy ndarray containing the values.

        Raises:
          ValueError: If the node isn't a Const.
        """
        if node_def.op != "Const":
            raise ValueError("Node named '%s' should be a Const op for values_from_const." %
                             node_def.name)
        input_tensor = node_def.attr["value"].tensor
        tensor_value = tensor_util.MakeNdarray(input_tensor)
        return tensor_value


def read_graph(in_graph, in_graph_is_binary=True):
    """Reads input graph file as GraphDef.

    :param in_graph: input graph file.
    :param in_graph_is_binary: whether input graph is binary, default True.
    :return: input graphDef.
    """
    if not gfile.Exists(in_graph):
        raise ValueError('Input graph pb file %s does not exist.' % in_graph)

    input_graph_def = graph_pb2.GraphDef()
    mode = "rb" if in_graph_is_binary else "r"
    with gfile.Open(in_graph, mode) as f:
        data = f.read()
        if in_graph_is_binary:
            input_graph_def.ParseFromString(data)
        else:
            text_format.Merge(data, input_graph_def)

    return input_graph_def


def write_graph(out_graph_def, out_graph_file):
    """Write output graphDef to file.

    :param out_graph_def: output graphDef.
    :param out_graph_file: path to output graph file.
    :return: None.
    """
    if not isinstance(out_graph_def, tf.compat.v1.GraphDef):
        raise ValueError('out_graph_def is not instance of TensorFlow GraphDef.')
    if out_graph_file and not os.path.exists(os.path.dirname(out_graph_file)):
        raise ValueError('"output_graph" directory does not exists.')
    f = gfile.GFile(out_graph_file, 'wb')
    f.write(out_graph_def.SerializeToString())


def is_ckpt_format(model_path):
    """check the model_path format is ckpt or not.

    Args:
        model_path (string): the model folder path

    Returns:
        string: return the ckpt prefix if the model_path contains ckpt format data else None.
    """
    file_list = [os.path.splitext(i)[-1] for i in os.listdir(model_path)]
    if file_list.count('.meta') == 1 and file_list.count('.index') == 1:
        return [os.path.splitext(i)[0] for i in os.listdir(model_path) if i.endswith(".meta")][0]
    else:
        return None


def parse_ckpt_model(ckpt_prefix, outputs):
    """Parse the ckpt model

    Args:
        ckpt_prefix (string): the ckpt prefix for parsing
    """
    with tf.compat.v1.Session() as sess:
        saver = tf.compat.v1.train.import_meta_graph(ckpt_prefix + '.meta', clear_devices=True)
        sess.run(tf.compat.v1.global_variables_initializer())
        saver.restore(sess, ckpt_prefix)
        graph_def = sess.graph.as_graph_def()
        _parse_ckpt_bn_input(graph_def)

        output_graph_def = graph_util.convert_variables_to_constants(sess=sess,
                                                                     input_graph_def=graph_def,
                                                                     output_node_names=outputs)

        return output_graph_def


def _parse_ckpt_bn_input(graph_def):
    """parse ckpt batch norm inputs to match correct moving mean and variance
    Args:
        graph_def (graph_def): original graph_def
    Returns:
        graph_def: well linked graph_def
    """
    for node in graph_def.node:
        if node.op == 'FusedBatchNorm':
            moving_mean_op_name = node.input[3]
            moving_var_op_name = node.input[4]
            moving_mean_op = _get_nodes_from_name(moving_mean_op_name, graph_def)[0]
            moving_var_op = _get_nodes_from_name(moving_var_op_name, graph_def)[0]

            if moving_mean_op.op == 'Const':
                name_part = moving_mean_op_name.rsplit('/', 1)[0]
                real_moving_mean_op_name = name_part + '/moving_mean'
                if len(_get_nodes_from_name(real_moving_mean_op_name, graph_def)) > 0:
                    # replace the real moving mean op name
                    node.input[3] = real_moving_mean_op_name

            if moving_var_op.op == 'Const':
                name_part = moving_var_op_name.rsplit('/', 1)[0]
                real_moving_var_op_name = name_part + '/moving_variance'
                if len(_get_nodes_from_name(real_moving_var_op_name, graph_def)) > 0:
                    # replace the real moving mean op name
                    node.input[4] = real_moving_var_op_name

    return graph_def


def _get_nodes_from_name(node_name, graph_def):
    """get nodes from graph_def using node name
    Args:
        graph_def (graph_def): graph_def
        node_name (str): node name

    Returns:
        node (NodeDef): graph node
    """
    return [node for node in graph_def.node if node.name == node_name]


def is_saved_model_format(model_path):
    """check the model_path format is saved_model or not

    Args:
        model_path (string): the model folder path

    Returns:
        bool: return True if the model_path contains saved_model format else False.
    """
    file_list = [os.path.splitext(i)[-1] for i in os.listdir(model_path)]
    if file_list.count('.pb') == 1 and ('variables') in os.listdir(model_path):
        return True
    else:
        return False


def parse_kerasmodel_model(model):
    """Convert Keras Model to graphdef

    Args:
        model (keras.Model): Keras model object

    Returns:
        graph_def: the parsed graph_def object.
        input_names: input node names
        output_names: output node name
    """
    full_model = tf.function(lambda *args: model(*args))
    concrete_function = full_model.get_concrete_function(model.inputs)
    frozen_model = convert_variables_to_constants_v2(concrete_function)
    graph_def = frozen_model.graph.as_graph_def()
    input_names = [node.name for node in graph_def.node if node.op == 'Placeholder']
    output_names = [output.name.split(':')[0] for output in model.outputs]
    return frozen_model.graph.as_graph_def(), input_names, output_names


def parse_savedmodel_model(model_path):
    """Convert SavedModel to graphdef

    Args:
        model_path (string): the model folder path

    Returns:
        graphdef: the parsed graphdef object.
        input_names: input node names
        output_names: output node name
    """

    with tf.compat.v1.Session() as sess:
        sess.run(tf.compat.v1.global_variables_initializer())

        meta_graph = tf.compat.v1.saved_model.loader.load(sess, ["serve"], model_path)

        model_graph_signature = list(meta_graph.signature_def.items())[0][1]

        input_names = [input_item[1].name for input_item in model_graph_signature.inputs.items()]

        output_names = [
            output_item[1].name for output_item in model_graph_signature.outputs.items()
        ]

        output_graph_def = graph_util.convert_variables_to_constants(
            sess=sess,
            input_graph_def=sess.graph_def,
            output_node_names=[
                output_item[0] for output_item in model_graph_signature.outputs.items()
            ])

        return output_graph_def, input_names, output_names


def convert_pb_to_savedmodel(graph_def, input_tensor_names, output_tensor_names, output_dir):
    """Convert the graphdef to SavedModel

    Args:
        graph_def (graphdef): parsed graphdef object.
        input_tensor_names (list): input tensor names list.
        output_tensor_names (list): output tensor names list.
        output_dir (string): Converted SavedModel store path.
    """
    builder = tf.compat.v1.saved_model.builder.SavedModelBuilder(output_dir)

    sigs = {}
    with tf.compat.v1.Session() as sess:
        tf.import_graph_def(graph_def, name="")
        g = tf.compat.v1.get_default_graph()

        input_tensors = {}
        for input_tensor_name in output_tensor_names:
            input_tensors[input_tensor_name.split(':')[0]] = g.get_tensor_by_name(
                "{}".format(input_tensor_name))

        output_tensors = {}
        for output_tensor_name in input_tensor_names:
            output_tensors[output_tensor_name.split(':')[0]] = g.get_tensor_by_name(
                "{}".format(output_tensor_name))

        sigs[signature_constants.DEFAULT_SERVING_SIGNATURE_DEF_KEY] = \
            tf.compat.v1.saved_model.signature_def_utils.predict_signature_def(
            output_tensors, input_tensors)

        builder.add_meta_graph_and_variables(sess, [tag_constants.SERVING], signature_def_map=sigs)

    builder.save()


def get_graph_def(model, outputs=[]):
    """Get the input model graphdef

    Args:
        model ([Graph, GraphDef or Path String]): The model could be the graph, graph_def object,
                the frozen pb or ckpt/savedmodel folder path.
        outputs ([String]): output node names list.

    Returns:
        graph_def (graphdef): parsed graphdef object.
    """
    graph_def = None
    if isinstance(model, Graph):
        graph_def = model.as_graph_def()
    elif isinstance(model, tf.compat.v1.GraphDef):
        graph_def = model
    elif isinstance(model, tf.keras.Model):
        graph_def, _, _ = parse_kerasmodel_model(model)
    elif isinstance(model, str):
        graph_def = tf.compat.v1.GraphDef()
        if model.endswith(".pb") and os.path.isfile(model):
            with open(model, "rb") as f:
                graph_def.ParseFromString(f.read())
        elif os.path.isdir(model):
            ckpt_prefix = is_ckpt_format(model)
            if ckpt_prefix:
                graph_def = parse_ckpt_model(os.path.join(model, ckpt_prefix), outputs)
            elif is_saved_model_format(model):
                graph_def, _, _ = parse_savedmodel_model(model)
            else:
                raise ValueError('Failed to parse ckpt model.')
        else:
            raise ValueError('The input model format is neither pb nor ckpt format.')

    else:
        raise ValueError('The input parameter is neither Graph nor path to the model.')

    return graph_def