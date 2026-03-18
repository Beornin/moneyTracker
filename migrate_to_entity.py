"""
Data Migration Script: Payee + PayeeRule → Entity
This script safely migrates the existing database schema to the new simplified Entity model.

IMPORTANT: Backup your database before running this script!

Usage:
    python migrate_to_entity.py
"""

import json
from datetime import datetime
from app import app, db
from sqlalchemy import text

def backup_database():
    """Create a JSON backup of critical data before migration."""
    print("📦 Creating data backup...")
    
    with app.app_context():
        backup = {
            'timestamp': datetime.now().isoformat(),
            'payees': [],
            'payee_rules': [],
            'transactions': []
        }
        
        # Backup Payees
        payees = db.session.execute(text("SELECT * FROM payee")).fetchall()
        for p in payees:
            backup['payees'].append(dict(p._mapping))
        
        # Backup PayeeRules
        rules = db.session.execute(text("SELECT * FROM payee_rule")).fetchall()
        for r in rules:
            backup['payee_rules'].append(dict(r._mapping))
        
        # Backup Transaction foreign keys
        txs = db.session.execute(text("SELECT id, payee_id, category_id FROM transaction")).fetchall()
        for t in txs:
            backup['transactions'].append(dict(t._mapping))
        
        # Save to file
        with open('migration_backup.json', 'w') as f:
            json.dump(backup, f, indent=2, default=str)
        
        print(f"✅ Backed up {len(backup['payees'])} payees, {len(backup['payee_rules'])} rules, {len(backup['transactions'])} transactions")
        return backup

def create_entity_table():
    """Create the new Entity table."""
    print("\n🔨 Creating Entity table...")
    
    with app.app_context():
        # Drop if exists (for clean migration)
        db.session.execute(text("DROP TABLE IF EXISTS entity CASCADE"))
        
        # Create Entity table
        create_sql = """
        CREATE TABLE entity (
            id SERIAL PRIMARY KEY,
            name VARCHAR(200) NOT NULL UNIQUE,
            category_id INTEGER NOT NULL REFERENCES category(id),
            match_patterns JSONB DEFAULT '[]'::jsonb,
            match_type VARCHAR(20) DEFAULT 'any',
            is_auto_created BOOLEAN DEFAULT FALSE,
            notes TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
        """
        db.session.execute(text(create_sql))
        
        # Create indexes
        db.session.execute(text("CREATE INDEX idx_entity_name ON entity(name)"))
        db.session.execute(text("CREATE INDEX idx_entity_category ON entity(category_id)"))
        db.session.execute(text("CREATE INDEX idx_entity_auto ON entity(is_auto_created)"))
        
        db.session.commit()
        print("✅ Entity table created")

def migrate_data():
    """Migrate data from Payee + PayeeRule to Entity."""
    print("\n🔄 Migrating data...")
    
    with app.app_context():
        # Get uncategorized ID
        uncat_result = db.session.execute(text("SELECT id FROM category WHERE name = 'Uncategorized'")).fetchone()
        uncat_id = uncat_result[0] if uncat_result else None
        
        if not uncat_id:
            print("⚠️  Creating Uncategorized category...")
            db.session.execute(text("INSERT INTO category (name, type) VALUES ('Uncategorized', 'Expense') RETURNING id"))
            uncat_result = db.session.execute(text("SELECT id FROM category WHERE name = 'Uncategorized'")).fetchone()
            uncat_id = uncat_result[0]
            db.session.commit()
        
        # Step 1: Create Entities from PayeeRules (merge duplicate display_names)
        rules = db.session.execute(text("""
            SELECT id, fragment, display_name, category_id, match_type 
            FROM payee_rule 
            ORDER BY display_name, id
        """)).fetchall()
        
        # Group rules by display_name to merge patterns
        entity_groups = {}
        for rule in rules:
            rule_id, fragment, display_name, category_id, match_type = rule
            
            if display_name not in entity_groups:
                entity_groups[display_name] = {
                    'patterns': [],
                    'rule_ids': [],
                    'category_id': category_id,
                    'match_type': match_type
                }
            
            entity_groups[display_name]['patterns'].append(fragment)
            entity_groups[display_name]['rule_ids'].append(rule_id)
        
        rule_to_entity = {}  # Maps old rule_id to new entity_id
        
        # Create one entity per unique display_name
        for display_name, data in entity_groups.items():
            result = db.session.execute(text("""
                INSERT INTO entity (name, category_id, match_patterns, match_type, is_auto_created)
                VALUES (:name, :cat_id, :patterns, :match_type, FALSE)
                RETURNING id
            """), {
                'name': display_name,
                'cat_id': data['category_id'],
                'patterns': json.dumps(data['patterns']),
                'match_type': data['match_type']
            })
            entity_id = result.fetchone()[0]
            
            # Map all rules with this display_name to the same entity
            for rule_id in data['rule_ids']:
                rule_to_entity[rule_id] = entity_id
        
        db.session.commit()
        print(f"✅ Created {len(entity_groups)} entities from {len(rules)} rules")
        
        # Step 2: Handle Payees without rules (auto-created entities)
        payees_no_rule = db.session.execute(text("""
            SELECT id, name 
            FROM payee 
            WHERE rule_id IS NULL
        """)).fetchall()
        
        payee_to_entity = {}  # Maps old payee_id to new entity_id
        
        for payee in payees_no_rule:
            payee_id, name = payee
            
            # Check if entity with this name already exists
            existing = db.session.execute(text("""
                SELECT id FROM entity WHERE name = :name
            """), {'name': name}).fetchone()
            
            if existing:
                payee_to_entity[payee_id] = existing[0]
            else:
                # Create auto-entity
                result = db.session.execute(text("""
                    INSERT INTO entity (name, category_id, match_patterns, is_auto_created)
                    VALUES (:name, :cat_id, '[]'::jsonb, TRUE)
                    RETURNING id
                """), {
                    'name': name,
                    'cat_id': uncat_id
                })
                entity_id = result.fetchone()[0]
                payee_to_entity[payee_id] = entity_id
        
        db.session.commit()
        print(f"✅ Created {len(payee_to_entity)} auto-entities from unmatched payees")
        
        # Step 3: Handle Payees with rules (map to existing entities)
        payees_with_rule = db.session.execute(text("""
            SELECT id, rule_id 
            FROM payee 
            WHERE rule_id IS NOT NULL
        """)).fetchall()
        
        for payee in payees_with_rule:
            payee_id, rule_id = payee
            if rule_id in rule_to_entity:
                payee_to_entity[payee_id] = rule_to_entity[rule_id]
        
        print(f"✅ Mapped {len(payees_with_rule)} payees to rule-based entities")
        
        # Step 4: Add entity_id column to transaction table
        print("\n🔧 Updating Transaction table...")
        
        db.session.execute(text("""
            ALTER TABLE transaction 
            ADD COLUMN IF NOT EXISTS entity_id INTEGER
        """))
        db.session.commit()
        
        # Step 5: Migrate transaction.payee_id → transaction.entity_id
        print("🔄 Migrating transaction references...")
        
        for payee_id, entity_id in payee_to_entity.items():
            db.session.execute(text("""
                UPDATE transaction 
                SET entity_id = :entity_id 
                WHERE payee_id = :payee_id
            """), {'entity_id': entity_id, 'payee_id': payee_id})
        
        db.session.commit()
        
        # Verify migration
        null_count = db.session.execute(text("""
            SELECT COUNT(*) FROM transaction WHERE entity_id IS NULL
        """)).fetchone()[0]
        
        if null_count > 0:
            print(f"⚠️  WARNING: {null_count} transactions have NULL entity_id")
        else:
            print("✅ All transactions migrated successfully")
        
        # Step 6: Make entity_id NOT NULL and add foreign key
        print("🔧 Adding constraints...")
        
        db.session.execute(text("""
            ALTER TABLE transaction 
            ALTER COLUMN entity_id SET NOT NULL
        """))
        
        db.session.execute(text("""
            ALTER TABLE transaction 
            ADD CONSTRAINT fk_transaction_entity 
            FOREIGN KEY (entity_id) REFERENCES entity(id)
        """))
        
        db.session.execute(text("""
            CREATE INDEX IF NOT EXISTS idx_tx_entity ON transaction(entity_id)
        """))
        
        db.session.commit()
        print("✅ Constraints added")
        
        return len(rule_to_entity) + len(payee_to_entity)

def cleanup_old_tables():
    """Drop old Payee and PayeeRule tables."""
    print("\n🧹 Cleaning up old tables...")
    
    response = input("Drop old Payee and PayeeRule tables? This cannot be undone! (yes/no): ")
    
    if response.lower() != 'yes':
        print("⏭️  Skipping cleanup. Old tables preserved.")
        return
    
    with app.app_context():
        # Drop old foreign key from transaction
        db.session.execute(text("""
            ALTER TABLE transaction 
            DROP CONSTRAINT IF EXISTS transaction_payee_id_fkey
        """))
        
        # Drop old column
        db.session.execute(text("""
            ALTER TABLE transaction 
            DROP COLUMN IF EXISTS payee_id
        """))
        
        # Drop old tables
        db.session.execute(text("DROP TABLE IF EXISTS payee CASCADE"))
        db.session.execute(text("DROP TABLE IF EXISTS payee_rule CASCADE"))
        
        db.session.commit()
        print("✅ Old tables removed")

def verify_migration():
    """Verify the migration was successful."""
    print("\n✅ Verification Report:")
    
    with app.app_context():
        entity_count = db.session.execute(text("SELECT COUNT(*) FROM entity")).fetchone()[0]
        auto_count = db.session.execute(text("SELECT COUNT(*) FROM entity WHERE is_auto_created = TRUE")).fetchone()[0]
        rule_count = db.session.execute(text("SELECT COUNT(*) FROM entity WHERE is_auto_created = FALSE")).fetchone()[0]
        tx_count = db.session.execute(text("SELECT COUNT(*) FROM transaction")).fetchone()[0]
        tx_with_entity = db.session.execute(text("SELECT COUNT(*) FROM transaction WHERE entity_id IS NOT NULL")).fetchone()[0]
        
        print(f"  • Total Entities: {entity_count}")
        print(f"  • Rule-based Entities: {rule_count}")
        print(f"  • Auto-created Entities: {auto_count}")
        print(f"  • Total Transactions: {tx_count}")
        print(f"  • Transactions with Entity: {tx_with_entity}")
        
        if tx_count == tx_with_entity:
            print("\n✅ Migration successful! All transactions have valid entities.")
        else:
            print(f"\n⚠️  WARNING: {tx_count - tx_with_entity} transactions missing entities!")

def main():
    """Main migration workflow."""
    print("=" * 60)
    print("  MIGRATION: Payee + PayeeRule → Entity")
    print("=" * 60)
    
    print("\n⚠️  WARNING: This will modify your database structure!")
    print("⚠️  A backup will be created, but please ensure you have a separate DB backup.")
    
    response = input("\nProceed with migration? (yes/no): ")
    
    if response.lower() != 'yes':
        print("❌ Migration cancelled.")
        return
    
    try:
        # Step 1: Backup
        backup_database()
        
        # Step 2: Create new table
        create_entity_table()
        
        # Step 3: Migrate data
        total_entities = migrate_data()
        
        # Step 4: Verify
        verify_migration()
        
        # Step 5: Cleanup (optional)
        cleanup_old_tables()
        
        print("\n" + "=" * 60)
        print("✅ MIGRATION COMPLETE!")
        print("=" * 60)
        print("\nNext steps:")
        print("1. Review migration_backup.json")
        print("2. Test the application thoroughly")
        print("3. Update app.py to use the new Entity model")
        
    except Exception as e:
        print(f"\n❌ Migration failed: {e}")
        print("Please check migration_backup.json and restore if needed.")
        raise

if __name__ == '__main__':
    main()
