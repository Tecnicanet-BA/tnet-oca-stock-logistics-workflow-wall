import re
from collections import namedtuple

from odoo import fields, models


class StockMove(models.Model):
    _inherit = "stock.move"

    # store the first group the move was in when created, used to keep track of
    # original group's name when creating a joint group for merged transfers,
    # and for cancellation of a sales order (to cancel only the moves related
    # to it)
    original_group_id = fields.Many2one(
        comodel_name="procurement.group", string="Original Procurement Group",
    )

    def _assign_picking(self):
        result = super(
            StockMove, self.with_context(picking_no_overwrite_partner_origin=1)
        )._assign_picking()
        self._on_assign_picking_merge_group()
        return result

    def _assign_picking_post_process(self, new=False):
        res = super()._assign_picking_post_process(new=new)
        if not new:
            self._on_assign_picking_message_link()
        return res

    def _prepare_merge_group_values(self, move_groups):
        sales = move_groups.sale_id
        return {
            "sale_ids": [(6, 0, sales.ids)],
            "name": ", ".join(move_groups.mapped("name")),
        }

    def _on_assign_picking_merge_group(self):
        for picking in self.picking_id:
            if not picking.picking_type_id.group_pickings:
                continue
            if picking.picking_type_id.code != "outgoing":
                continue
            base_group = picking.group_id
            # If we have different sales in the line's group, it means moves
            # have been merged in the same picking/group but they come from a
            # different sale.
            moves = picking.move_lines
            moves_groups = moves.original_group_id
            moves_sales = moves_groups.sale_id
            group_sales = base_group.sale_ids
            # if we have different sales, it means "_assign_picking" added
            # moves from another SO in the picking
            if moves_sales != group_sales:
                # create a new joint group for the existing different groups
                new_group = base_group.copy(
                    self._prepare_merge_group_values(moves_groups)
                )
                pickings = base_group.picking_ids.filtered(
                    lambda picking: picking.picking_type_id.group_pickings
                    # Do no longer modify a printed or done transfer: they are
                    # started and their group is now fixed. It prevents keeping
                    # old, done sales orders in new groups forever
                    and not (picking.printed or picking.state == "done")
                )
                pickings.move_lines.group_id = new_group

    def _on_assign_picking_message_link(self):
        picking = self.picking_id
        picking.ensure_one()
        sales = self.mapped("sale_line_id.order_id")
        for sale in sales:
            pattern = r"\b%s\b" % sale.name
            if not re.search(pattern, picking.origin):
                picking.origin += " " + sale.name
                picking.message_post_with_view(
                    "mail.message_origin_link",
                    values={"self": picking, "origin": sale},
                    subtype_id=self.env.ref("mail.mt_note").id,
                )

    def _search_picking_for_assignation(self):
        # totally reimplement this one to add a hook to change the domain
        self.ensure_one()
        picking = self.env["stock.picking"].search(
            self._domain_search_picking_for_assignation(), limit=1
        )
        return picking

    def _domain_search_picking_handle_move_type(self):
        """Hook to handle the move type. Can be overloaded by other modules.
        By default the move type is taken from the procurement group.
        """
        # avoid mixing picking policies
        return [("move_type", "=", self.group_id.move_type)]

    def _domain_search_picking_for_assignation(self):
        states = ("draft", "confirmed", "waiting", "partially_available", "assigned")
        if (
            not self.picking_type_id.group_pickings
            or self.group_id.sale_id.picking_policy == "one"
        ):
            # use the normal domain from the stock module
            domain = [
                ("group_id", "=", self.group_id.id),
            ]
        else:
            domain = [
                # same partner
                ("partner_id", "=", self.group_id.partner_id.id),
                # don't search on the procurement.group
            ]
            domain += self._domain_search_picking_handle_move_type()
            # same carrier only for outgoing transfers
            if self.picking_type_id.code == "outgoing":
                domain += [
                    ("carrier_id", "=", self.group_id.carrier_id.id),
                ]
            else:
                domain += [("carrier_id", "=", False)]
        domain += [
            ("location_id", "=", self.location_id.id),
            ("location_dest_id", "=", self.location_dest_id.id),
            ("picking_type_id", "=", self.picking_type_id.id),
            ("printed", "=", False),
            ("immediate_transfer", "=", False),
            ("state", "in", states),
        ]
        if self.env.context.get("picking_no_copy_if_can_group"):
            # we are in the context of the creation of a backorder:
            # don't consider the current move's picking
            domain.append(("id", "!=", self.picking_id.id))
        return domain

    def _key_assign_picking(self):
        return (
            self.sale_line_id.order_id.partner_shipping_id,
            PickingPolicy(id=self.sale_line_id.order_id.picking_policy),
        ) + super()._key_assign_picking()


# we define a named tuple because the code in module stock expects the values in
# the tuple returned by _key_assign_picking to be records with an id attribute
PickingPolicy = namedtuple("PickingPolicy", ["id"])
