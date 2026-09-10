"""Shared simulation commands. Transports only adapt names, auth and audit logs."""
from __future__ import annotations


class ControlError(ValueError):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.status = status


def integer(value, name, low, high):
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise ControlError(f"{name} must be an integer between {low} and {high}")
    return value


class RunControls:
    def __init__(self, session):
        self.session = session

    def current(self):
        if self.session.sim is None:
            raise ControlError("no run in progress; start or restart first", 409)
        return self.session.sim

    def parameters(self, body):
        aliases = {'columns': 'cols', 'tractors': 'carts', 'minObstacles': 'min_obstacles',
                   'maxObstacles': 'max_obstacles', 'newSeed': 'new_seed', 'priorityRegion': 'priority_region'}
        allowed = {'rows', 'cols', 'harvesters', 'carts', 'min_obstacles', 'max_obstacles', 'new_seed', 'priority_region'}
        result = {}
        for key, value in body.items():
            name = aliases.get(key, key)
            if name not in allowed:
                raise ControlError(f"unknown run parameter: {key}")
            if name in result and result[name] != value:
                raise ControlError(f"conflicting values for {name}")
            result[name] = value
        return result

    def command(self, action, body=None):
        body = body or {}
        if action in ('start', 'restart', 'config'):
            params = self.parameters(body)
            region = params.pop('priority_region', None)
            if region is not None and region not in ('north', 'south', 'east', 'west'):
                raise ControlError('priority_region must be north, south, east or west')
            applied = (self.session.start(**params) if action == 'start'
                       else self.session.restart(**params))
        elif action == 'reset':
            applied = self.session.reset()
        elif action == 'pause':
            self.session.pause()
            applied = self.session.parameters()
        elif action in ('resume', 'continue'):
            self.session.resume()
            applied = self.session.parameters()
        else:
            raise ControlError(f"unknown command: {action}", 404)
        result = {'status': self.session.status, 'queued': False, 'runId': self.session.run_id,
                  'applied': applied}
        if action in ('start', 'restart', 'config') and region is not None:
            result['priority'] = self.prioritize_direction(region)
        return result

    def rebalance(self):
        return self.current().rebalance()

    def policy(self, request_threshold=None, wait_weight=None):
        current = self.current()
        if request_threshold is None and wait_weight is None:
            raise ControlError('give request_threshold and/or wait_weight')
        for name, value in (('request_threshold', request_threshold), ('wait_weight', wait_weight)):
            if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float))):
                raise ControlError(f'{name} must be a number')
        return current.set_policy(request_threshold=request_threshold, wait_weight=wait_weight)

    def add_cart(self):
        current = self.current()
        if len(current.carts) >= 6:
            raise ControlError('six carts is the most this field holds without gridlock', 409)
        return {'cart': f'C{current.add_cart()}', 'fleet_carts': len(current.carts)}

    def prioritize_direction(self, region):
        current = self.current()
        rows, cols = current.field.rows, current.field.cols
        bounds = {
            'north': (0, 0, (rows - 1) // 2, cols - 1),
            'south': (rows // 2, 0, rows - 1, cols - 1),
            'west': (0, 0, rows - 1, (cols - 1) // 2),
            'east': (0, cols // 2, rows - 1, cols - 1),
        }
        if region not in bounds:
            raise ControlError('region must be north, south, east or west')
        return self.prioritize(*bounds[region])

    def prioritize(self, top_row, left_column, bottom_row, right_column):
        current = self.current()
        top = integer(top_row, 'top_row', 0, current.field.rows - 1)
        bottom = integer(bottom_row, 'bottom_row', 0, current.field.rows - 1)
        left = integer(left_column, 'left_column', 0, current.field.cols - 1)
        right = integer(right_column, 'right_column', 0, current.field.cols - 1)
        if top > bottom or left > right:
            raise ControlError('region corners must be ordered from top-left to bottom-right')
        promoted = current.prioritize((top, left), (bottom, right))
        return {'region': {'top': top, 'left': left, 'bottom': bottom, 'right': right},
                'cells_promoted': promoted,
                'note': 'the fleet works this region first' if promoted else 'no standing crop inside that region'}

    def machine(self, action, harvester):
        current = self.current()
        if isinstance(harvester, str):
            try:
                harvester = int(harvester.strip().upper().removeprefix('H'))
            except ValueError:
                raise ControlError("harvester id like 'H1' or 1") from None
        target = integer(harvester, 'harvester', 0, len(current.harvesters) - 1)
        if action == 'disable':
            if not current.by_id[target].disabled and sum(not h.disabled for h in current.harvesters) <= 1:
                raise ControlError('that is the last running harvester', 409)
            changed = current.disable(target)
        elif action == 'repair':
            changed = current.repair(target)
        else:
            raise ControlError(f'unknown machine action: {action}', 404)
        return {'harvester': f'H{target}', 'changed': changed,
                'zones': [{'harvester': h.label, 'crop': len(h.plan)} for h in current.harvesters if not h.disabled]}

    def announce(self, text):
        if not isinstance(text, str) or not 1 <= len(text.strip()) <= 160:
            raise ControlError('text must be 1 to 160 characters')
        self.session.narration = {'text': text.strip(), 'tick': self.session.sim.tick if self.session.sim else 0}
        return {'shown': text.strip()}
