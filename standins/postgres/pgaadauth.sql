-- Stands in for the function Azure Database for PostgreSQL ships to map an Entra identity to
-- a database role (#81). Same signature. The real one also records the object ID so Azure
-- can check the identity's token; this one only makes a plain login role, which is enough to
-- rehearse the deploy against a local or CI PostgreSQL. Never load this into Azure.
create or replace function pgaadauth_create_principal_with_oid(
  role_name text, object_id text, object_type text, is_admin boolean, is_mfa boolean)
returns text language plpgsql as $$
begin
  if object_type <> 'service' or is_admin or is_mfa then
    raise exception 'unexpected principal % % % %', object_id, object_type, is_admin, is_mfa;
  end if;
  execute format('create role %I login', role_name);
  return 'Created role for ' || role_name;
end $$;
