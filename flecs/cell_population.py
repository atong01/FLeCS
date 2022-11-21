from __future__ import annotations
from abc import ABC, abstractmethod
import flecs.sets as sets
from typing import Tuple, Dict, Union, List
from flecs.data.utils import load_interaction_data
from flecs.data.interaction_data import InteractionData, SetData, EdgeType
from torch.distributions.normal import Normal
from flecs.decay import exponential_decay
from flecs.production import SimpleConv
import torch


########################################################################################################################
# Cell Population abstract class
########################################################################################################################


class CellPopulation(ABC, torch.nn.Module):
    def __init__(
        self,
        interaction_graph: InteractionData,
        n_cells: int = 1,
        per_node_state_dim: int = 1,
    ):
        """
        A population of cells. The mechanisms of cells are based on a graph with different types of nodes and edges.
        Cell dynamics can be computed based on these mechanisms.

        * Examples of node types include "proteins", "small molecules", "gene/RNA".
        * Examples of edge types include ("gene", "activates", "gene"), ("protein", "catalyses", "small molecule").

        All nodes/edges of a given type are grouped in a `NodeSet`/`EdgeSet` object.

        A specific node usually corresponds to a specific molecule (e.g. RNA from gene X) whose concentration
        (and potentially other properties) is tracked. Together, the tracked properties of all the nodes define the
        state of the cell.

        Production rates and Decay rates (for all the tracked properties of all the nodes) can be computed and depend
        on the state of the cell, as well as some node parameters and edge parameters.

        To define your own `CellPopulation` class inheriting from this class, you need to implement the two methods
        `compute_production_rates` and `compute_decay_rates`. You may also want to override
        `sample_from_state_prior_dist` in order to choose your own prior distribution over the state of cells.

        Args:
            interaction_graph: Graph on which the mechanisms of cells will be based.
            n_cells: Number of cells in the population.
            per_node_state_dim: Dimension of the state associated with each node.
        """
        super().__init__()
        # str type of node (e.g., gene, protein).
        self._node_set_dict: Dict[str, sets.NodeSet] = {}
        # str types of interactions (src, interaction_type, dest).
        self._edge_set_dict: Dict[Tuple[str, str, str], sets.EdgeSet] = {}

        self.initialize_from_interaction_graph(interaction_graph)

        # Create state and production/decay rates as empty tensors
        self.state = torch.empty((n_cells, self.n_nodes, per_node_state_dim))
        self.decay_rates = torch.empty((n_cells, self.n_nodes, per_node_state_dim))
        self.production_rates = torch.empty((n_cells, self.n_nodes, per_node_state_dim))

        # Initialize
        self.reset_state()

    def sample_from_state_prior_dist(self) -> torch.Tensor:
        """
        Method which will get called to (re)-initialize the state of the cell population.

        Returns:
            Tensor with the same shape as `self.state`
        """
        SCALE_FACTOR = 10  # Arbitrarily initialize the state to 10
        return SCALE_FACTOR * torch.ones(self.state.shape)

    def reset_state(self):
        """
        Resets the state, production_rates and decay_rates attributes of the cell population.
        """
        self.state = self.sample_from_state_prior_dist()
        self.production_rates = torch.empty(self.production_rates.shape)
        self.decay_rates = torch.empty(self.decay_rates.shape)

    def __getitem__(
        self, key: Union[str, Tuple[str, str, str]]
    ) -> Union[sets.NodeSet, sets.EdgeSet]:
        if type(key) is tuple:
            return self._edge_set_dict[key]
        else:
            return self._node_set_dict[key]

    def __setitem__(
        self,
        key: Union[str, Tuple[str, str, str]],
        value: Union[sets.NodeSet, sets.EdgeSet],
    ):
        if type(key) is tuple:
            assert isinstance(value, sets.EdgeSet)
            assert key not in self._edge_set_dict
            self._edge_set_dict[key] = value
        else:
            assert isinstance(value, sets.NodeSet)
            assert key not in self._node_set_dict
            self._node_set_dict[key] = value

    @property
    def n_cells(self) -> int:
        """
        (`int`): Number of cells in the population
        """
        return self.state.shape[0]

    @property
    def n_nodes(self) -> int:
        """
        (`int`): Number of nodes in the underlying cell mechanisms.
        """
        return sum([len(node_set) for node_set in self._node_set_dict.values()])

    @property
    def node_types(self) -> List[str]:
        """
        (`List[str]`): List the different types of nodes. Each node type is associated with a NodeSet object.
        """
        return list(self._node_set_dict.keys())

    @property
    def edge_types(self) -> List[Tuple[str, str, str]]:
        """
        (`List[str]`): List the different types of edges. Each edge type is associated with an EdgeSet object.
        """
        return list(self._edge_set_dict.keys())

    @abstractmethod
    def compute_production_rates(self) -> None:
        """
        Abstract method. Should update `self.production_rates`
        """
        pass

    @abstractmethod
    def compute_decay_rates(self) -> None:
        """
        Abstract method. Should update `self.decay_rates`
        """
        pass

    def get_production_rates(self) -> torch.Tensor:
        """
        Computes and returns the production rates of the system.
        """
        self.compute_production_rates()
        return self.production_rates

    def get_decay_rates(self) -> torch.Tensor:
        """
        Computes and returns the decay rates of the system.
        """
        self.compute_decay_rates()
        return self.decay_rates

    def get_derivatives(self, state: torch.Tensor) -> torch.Tensor:
        """
        Computes and returns the time derivatives of the system for a given state.

        Args:
            state: State of the Cell Population for which derivatives should be computed.

        Returns:
            time derivatives of all the tracked properties of the Cell Population.
        """
        self.state = state
        return self.get_production_rates() - self.get_decay_rates()

    def _get_node_set(self, n_type_data: SetData) -> sets.NodeSet:
        """
        Given node type data Dict[AttributeName, AttributeList], returns a `NodeSet` with the associated attributes.
        """
        idx_low = int(min(n_type_data["idx"]))
        idx_high = int(max(n_type_data["idx"]))
        n_type_data.pop("idx", None)

        attr_dict = {
            k: v for k, v in n_type_data.items() if isinstance(v, torch.Tensor)
        }

        return sets.NodeSet(self, idx_low, idx_high, attribute_dict=attr_dict)

    def _get_edge_set(self, e_type: EdgeType, e_type_data: SetData) -> sets.EdgeSet:
        """
        Given edge type data Dict[AttributeName, AttributeList], returns an `EdgeSet` with the associated attributes.
        """
        edges = e_type_data["idx"]
        # We shift the edge tail/head indices by idx_low for the source/target node type
        edges[:, 0] -= self[e_type[0]].idx_low  # e_type[0] = Source
        edges[:, 1] -= self[e_type[2]].idx_low  # e_type[2] = Target
        e_type_data.pop("idx", None)

        attr_dict = {
            k: v for k, v in e_type_data.items() if isinstance(v, torch.Tensor)
        }

        return sets.EdgeSet(edges, attribute_dict=attr_dict)

    def initialize_from_interaction_graph(
        self, interaction_graph: InteractionData
    ) -> None:
        """
        Args:
            interaction_graph: Interaction graph from which `NodeSet` and `EdgeSet` objects should be initialized.
        """
        node_data_dict = interaction_graph.get_formatted_node_data()
        edge_data_dict = interaction_graph.get_formatted_edge_data()

        for n_type, n_type_data in node_data_dict.items():
            self[n_type] = self._get_node_set(n_type_data)

        for e_type, e_type_data in edge_data_dict.items():
            self[e_type] = self._get_edge_set(e_type, e_type_data)

    def set_production_rates_to_zero(self) -> None:
        """
        Sets production rates to zero.
        """
        for n_type in self.node_types:
            self[n_type].production_rate = torch.zeros(
                self[n_type].production_rate.shape
            )

    def parameters(self, recurse: bool = True):
        for k, n_set in self._node_set_dict.items():
            yield from n_set.parameters(recurse=recurse)
        for k, e_set in self._edge_set_dict.items():
            yield from e_set.parameters(recurse=recurse)
        for name, param in self.named_parameters(recurse=recurse):
            yield param

    def __repr__(self):
        return "CellPopulation. {} nodes and {} cells.\n".format(
            self.n_nodes, self.n_cells
        )

    def __str__(self):
        s = self.__repr__()

        s += "\tNodeSets:\n"
        for k, v in self._node_set_dict.items():
            s += "\t\t{}: {}\n".format(k, v)

        s += "\tEdgeSets:\n"
        for k, v in self._edge_set_dict.items():
            s += "\t\t{}: {}".format(k, v)

        return s


########################################################################################################################
# Cell Population classes
########################################################################################################################


class TestCellPop(CellPopulation):
    def __init__(self, n_cells: int = 1):
        """
        Basic Test Cell Population.

        Mechanisms are based on the calcium signaling pathway from KEGG:

        * 60 nodes and 57 edges.
        * 2 different types of nodes: ['compound', 'gene'].
        * 5 different types of interactions: ['', 'activation', 'binding/association', 'compound', 'inhibition'].

        Each edge type is associated with a graph convolution operation. Together these graph convolutions are used to
        compute the production rates:

        ```
        self[tgt_n_type].production_rate += self[e_type].simple_conv(
            x=self[src_n_type].state,
            edge_index=self[e_type].edges.T,
            edge_weight=self[e_type].weights,
        )
        ```

        Decay rates are exponential decays:

        ```
        self[n_type].decay_rate = exponential_decay(self, n_type, alpha=self[n_type].alpha)
        ```

        Args:
            n_cells: Number of cells in the population
        """
        interaction_graph = load_interaction_data("test")
        super().__init__(interaction_graph, n_cells=n_cells)

        # Initialize additional node attributes.
        self["gene"].init_param(name="alpha", dist=Normal(5, 0.01))
        self["compound"].init_param(name="alpha", dist=Normal(5, 0.01))

        # Initialize additional edge attributes.
        for e_type in self.edge_types:
            self[e_type].init_param(name="weights", dist=Normal(0, 1))
            self[e_type].simple_conv = SimpleConv(tgt_nodeset_len=len(self[e_type[2]]))

    def compute_production_rates(self):
        self.set_production_rates_to_zero()
        for e_type in self.edge_types:
            src_n_type, interaction_type, tgt_n_type = e_type
            self[tgt_n_type].production_rate += self[e_type].simple_conv(
                x=self[src_n_type].state,
                edge_index=self[e_type].edges.T,
                edge_weight=self[e_type].weights,
            )

    def compute_decay_rates(self):
        for n_type in self.node_types:
            self[n_type].decay_rate = exponential_decay(
                self, n_type, alpha=self[n_type].alpha
            )


class ProteinRNACellPop(CellPopulation):
    def __init__(self, n_cells: int = 1):
        """
        Cell Population which tracks the concentration of RNA and the concentration of protein for each gene.

        Mechanisms are based on the calcium signaling pathway from KEGG:

        * 60 nodes and 57 edges.
        * 2 different types of nodes: ['compound', 'gene'].
        * 5 different types of interactions: ['', 'activation', 'binding/association', 'compound', 'inhibition'].

        Each edge type is associated with a graph convolution operation. Together these graph convolutions are used to
        compute the production rates:

        For edges between genes, of type ("gene", *, "gene"),  messages are passed from the source proteins to the
        target RNA. This aims at modeling transcriptional regulation by Transcription Factor proteins:

        ```
        rna_prod_rate += self[e_type].simple_conv(
            x=protein_state,
            edge_index=self[e_type].edges.T,
            edge_weight=self[e_type].weights,
        )
        ```

        For the other types of edges, default graph convolutions are used:

        ```
        self[tgt_n_type].production_rate += self[e_type].simple_conv(
            x=self[src_n_type].state,
            edge_index=self[e_type].edges.T,
            edge_weight=self[e_type].weights,
        )
        ```

        Decay rates are exponential decays:

        ```
        self[n_type].decay_rate = exponential_decay(self, n_type, alpha=self[n_type].alpha)
        ```

        Args:
            n_cells: Number of cells in the population
        """
        interaction_graph = load_interaction_data("test")
        super().__init__(interaction_graph, n_cells=n_cells, per_node_state_dim=2)

        # Initialize additional node attributes.
        self["gene"].init_param(
            name="alpha", dist=Normal(5, 1), shape=(1, len(self["gene"]), 2)
        )
        self["gene"].init_param(
            name="translation_rate", dist=Normal(5, 1), shape=(1, len(self["gene"]), 1)
        )

        self["compound"].init_param(
            name="alpha", dist=Normal(5, 0.01), shape=(1, len(self["compound"]), 2)
        )

        # Initialize additional edge attributes.
        for e_type in self.edge_types:
            self[e_type].init_param(name="weights", dist=Normal(0, 1))
            self[e_type].simple_conv = SimpleConv(tgt_nodeset_len=len(self[e_type[2]]))

    def compute_production_rates(self):
        self.set_production_rates_to_zero()

        for e_type in self.edge_types:
            src_n_type, interaction_type, tgt_n_type = e_type

            if e_type[0] == e_type[2] == "gene":  # Edges between genes
                # RNA production depends on the concentration of parent proteins
                rna_prod_rate = self["gene"].production_rate[:, :, 0:1]
                protein_state = self["gene"].state[:, :, 1:2]

                rna_prod_rate += self[e_type].simple_conv(
                    x=protein_state,
                    edge_index=self[e_type].edges.T,
                    edge_weight=self[e_type].weights,
                )
            else:
                # Regular message passing
                self[tgt_n_type].production_rate += self[e_type].simple_conv(
                    x=self[src_n_type].state,
                    edge_index=self[e_type].edges.T,
                    edge_weight=self[e_type].weights,
                )

        # Protein production depends on the concentration of the RNA coding for that protein
        protein_prod_rate = self["gene"].production_rate[:, :, 1:2]
        protein_prod_rate += (
            self["gene"].translation_rate * self["gene"].state[:, :, 0:1]
        )

    def compute_decay_rates(self):
        for n_type in self.node_types:
            self[n_type].decay_rate = exponential_decay(
                self, n_type, alpha=self[n_type].alpha
            )


if __name__ == "__main__":
    from flecs.trajectory import simulate_deterministic_trajectory
    from flecs.utils import plot_trajectory, set_seed
    import matplotlib.pyplot as plt

    set_seed(0)

    # Simulate trajectories.
    cell_pop = ProteinRNACellPop()
    cell_traj = simulate_deterministic_trajectory(cell_pop, torch.linspace(0, 1, 100))

    plot_trajectory(cell_traj, legend=False)
    plt.show()
