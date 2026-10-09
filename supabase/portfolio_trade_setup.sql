-- Run once in Supabase SQL Editor. Real brokerage orders are NEVER sent.
create table if not exists public.portfolio_trades (
 id uuid primary key default gen_random_uuid(),
 user_id uuid not null references auth.users(id) on delete cascade,
 symbol text not null,
 side text not null check (side in ('buy','sell')),
 quantity numeric not null check (quantity > 0),
 price numeric not null check (price >= 0),
 traded_at timestamptz not null default now(),
 created_at timestamptz not null default now()
);
create index if not exists portfolio_trades_user_time on public.portfolio_trades(user_id,traded_at desc);
alter table public.portfolio_trades enable row level security;
revoke all on public.portfolio_trades from anon;
grant select on public.portfolio_trades to authenticated;
create policy "read own trades" on public.portfolio_trades for select to authenticated using (user_id = (select auth.uid()));
-- Atomic, server-validated changes to holdings + audit history.
create or replace function public.record_portfolio_trade(p_symbol text,p_side text,p_quantity numeric,p_price numeric)
returns uuid language plpgsql security definer set search_path = ''
as $$
declare v_uid uuid := auth.uid(); v_pos public.portfolio_positions%rowtype; v_id uuid; v_newqty numeric; v_newcost numeric;
begin
 if v_uid is null then raise exception 'Authentication required'; end if;
 if p_symbol is null or p_symbol !~ '^[0-9]{6}$' or p_side not in ('buy','sell')
 or p_quantity is null or p_quantity <= 0 or p_quantity <> trunc(p_quantity)
 or p_price is null or p_price < 0 then raise exception 'Invalid trade input'; end if;
 select * into v_pos from public.portfolio_positions where user_id=v_uid and symbol=p_symbol for update;
 if not found then raise exception 'Position not found; add new symbols separately'; end if;
 if p_side='sell' and v_pos.quantity < p_quantity then raise exception 'Insufficient position'; end if;
 v_newqty := case when p_side='buy' then v_pos.quantity+p_quantity else v_pos.quantity-p_quantity end;
 v_newcost := case when p_side='buy' then (v_pos.quantity*v_pos.avg_cost+p_quantity*p_price)/v_newqty
 when v_newqty=0 then 0 else v_pos.avg_cost end;
 update public.portfolio_positions set quantity=v_newqty,avg_cost=round(v_newcost,6),updated_at=now()
 where id=v_pos.id and user_id=v_uid;
 insert into public.portfolio_trades(user_id,symbol,side,quantity,price)
 values(v_uid,p_symbol,p_side,p_quantity,p_price) returning id into v_id;
 return v_id;
end $$;
revoke all on function public.record_portfolio_trade(text,text,numeric,numeric) from public,anon;
grant execute on function public.record_portfolio_trade(text,text,numeric,numeric) to authenticated;
