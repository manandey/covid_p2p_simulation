from typing import List, Mapping

import random
import datetime
import itertools

import simpy
import networkx as nx
from addict import Dict


from utils import _draw_random_discreet_gaussian, compute_distance, get_random_word
from config import TICK_MINUTE
import mobility_config as mcfg
import mobility_utils as mutl


class Env(simpy.Environment):
    def __init__(self, initial_timestamp):
        super().__init__()
        self.initial_timestamp = initial_timestamp

    def time(self):
        return self.now

    @property
    def timestamp(self):
        return self.initial_timestamp + datetime.timedelta(
            minutes=self.now * TICK_MINUTE
        )

    def minutes(self):
        return self.timestamp.minute

    def hour_of_day(self):
        return self.timestamp.hour

    def day_of_week(self):
        return self.timestamp.weekday()

    def is_weekend(self):
        return self.day_of_week() in [0, 6]

    def time_of_day(self):
        return self.timestamp.isoformat()


class Location(simpy.Resource):
    def __init__(
        self,
        env,
        capacity=simpy.core.Infinity,
        name="Safeway",
        location_type="stores",
        lat=None,
        lon=None,
        cont_prob=None,
    ):
        super().__init__(env, capacity)
        self.humans = set()
        self.name = name
        self.lat = lat
        self.lon = lon
        self.location_type = location_type
        self.cont_prob = cont_prob

    def sick_human(self):
        return any([h.is_sick for h in self.humans])

    def __repr__(self):
        return (
            f"{self.location_type}:{self.name} - "
            f"Total number of people in {self.location_type}:{len(self.humans)} "
            f"- sick:{self.sick_human()}"
        )

    def contamination_proba(self):
        # FIXME Contamination probability of a location should decay with time since the last
        #  infected human was around, at a rate that depends on the `location_type`.
        if not self.sick_human():
            return 0
        return self.cont_prob

    def __hash__(self):
        return hash(self.name)

    def __eq__(self, other):
        return self.name == other.name

    @classmethod
    def random_location(
        cls,
        env: Env,
        city_size: int = 1000,
        capacity: float = simpy.core.Infinity,
        cont_prob: float = None,
    ):
        location = cls(
            env=env,
            capacity=capacity,
            name=get_random_word(),
            lat=random.uniform(
                mcfg.DEFAULT_CITY.COORD.SOUTH.LAT, mcfg.DEFAULT_CITY.COORD.NORTH.LAT
            ),
            lon=random.uniform(
                mcfg.DEFAULT_CITY.COORD.WEST.LON, mcfg.DEFAULT_CITY.COORD.EAST.LON
            ),
            cont_prob=(cont_prob or random.uniform(0, 1)),
            location_type="misc",
        )
        return location


class Transit(Location):
    def __init__(
        self,
        env: Env,
        source: Location,
        destination: Location,
        mobility_mode: mcfg.MobilityMode,
    ):
        self.source = source
        self.destination = destination
        self.mobility_mode = mobility_mode
        super(Transit, self).__init__(
            env,
            capacity=mobility_mode.capacity,
            name=f"{source.name}--({mobility_mode.name})-->{destination.name}",
            location_type="transit",
            # FIXME This should entail counting the number of humans
            cont_prob=mobility_mode.transmission_proba,
        )


class City(object):
    def __init__(self, env: Env, locations: List[Location]):
        self.env = env
        self.locations = locations
        # Prepare a graph over locations
        self._build_graph()

    def _build_graph(self):
        graph = nx.MultiGraph()
        # Add stores, parks, households as nodes
        graph.add_nodes_from(self.locations)
        # Edges between nodes are annotated by mobility modes
        for source, destination in itertools.product(graph.nodes, graph.nodes):
            if (source, destination) in graph.edges:
                continue
            # To the edges, we're gonna add:
            #   1. Raw distance,
            #   2. A transit object (which is a location)
            raw_distance = mutl.compute_geo_distance(source, destination)
            for mobility_mode in mcfg.MobilityMode.get_all_mobility_modes():
                mobility_mode: mcfg.MobilityMode
                if mobility_mode.is_compatible_with_distance(distance=raw_distance):
                    graph.add_edge(
                        source,
                        destination,
                        mobility_mode,
                        transit=Transit(self.env, source, destination, mobility_mode),
                        raw_distance=raw_distance,
                    )
        self.graph = graph

    def plan_trip(
        self,
        source: Location,
        destination: Location,
        mobility_mode_preference: Mapping[mcfg.MobilityMode, int],
    ) -> List[Transit]:

        if destination == source:
            return []
        favorite_modes = Dict()

        # The weight function provides a measure of "distance" for Djikstra
        def weight_fn(u, v, d):
            global favorite_modes
            # First case is when the mobility mode is not supported
            if not set(d.keys()).intersection(set(mobility_mode_preference.keys())):
                # This means that mobility_mode_preference does not specify
                # a preference for this mode, so we assume that the edge cannot
                # be traversed. Returning None tells networkx just that.
                return None
            # TODO Make mode_weights depend on travel_time, which in turn depends
            #  on the speed of mobility mode.
            # We assume that the preference is a multiplier.
            raw_distance = d["raw_distance"]
            mode_favorabilities = {
                mode: mode.favorability_given_distance(raw_distance)
                for mode in mobility_mode_preference
            }
            mode_weights = {
                mode: favorability / mobility_mode_preference[mode]
                for mode, favorability in mode_favorabilities.items()
            }
            # Record the favorite mode
            min_weight = min(list(mode_weights.values()))
            favorite_mode = [
                mode for mode, weight in mode_weights.items() if weight == min_weight
            ][0]
            favorite_modes[u][v] = favorite_mode
            return min_weight

        try:
            # Now get that Djikstra path!
            djikstra_path = nx.dijkstra_path(
                self.graph, source, destination, weight=weight_fn
            )
        except nx.exception.NetworkXNoPath:
            # No path; destination might have to be resampled
            return []
        # Convert path to transits and return
        transits = []
        for transit_source, transit_destination in zip(
            djikstra_path, djikstra_path[1:]
        ):
            favorite_transit_mode = favorite_modes[transit_source][transit_destination]
            transit = self.graph[transit_source][transit_destination][
                favorite_transit_mode
            ]["transit"]
            transits.append(transit)

        return transits


if __name__ == "__main__":
    env = Env(datetime.datetime(2020, 2, 28, 0, 0))
    city = City(env, [Location.random_location(env) for _ in range(100)])
    # noinspection PyTypeChecker
    plan = city.plan_trip(
        source=city.locations[0],
        destination=city.locations[1],
        mobility_mode_preference={mcfg.WALKING: 2.0, mcfg.BUS: 1.0},
    )
