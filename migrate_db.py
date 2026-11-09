from sqlalchemy import create_engine, text
from app.core.config import settings

def migrate():
    engine = create_engine(settings.MYSQL_URL)
    with engine.connect() as conn:
        trans = conn.begin()
        try:
            print("Applying migrations to 'claims' table...")

            # 1. Ensure review_reason exists
            try:
                conn.execute(text("ALTER TABLE claims ADD COLUMN review_reason TEXT"))
                print("Added column 'review_reason'")
            except Exception as e:
                if "Duplicate column name" in str(e):
                    print("Column 'review_reason' already exists")
                else:
                    raise e

            # 2. Ensure confidentiality_status exists
            try:
                conn.execute(text("ALTER TABLE claims ADD COLUMN confidentiality_status VARCHAR(20)"))
                print("Added column 'confidentiality_status'")
            except Exception as e:
                if "Duplicate column name" in str(e):
                    print("Column 'confidentiality_status' already exists")
                else:
                    raise e

            # 3. Add confidence_value (the new float column)
            try:
                conn.execute(text("ALTER TABLE claims ADD COLUMN confidence_value FLOAT"))
                print("Added column 'confidence_value'")
            except Exception as e:
                if "Duplicate column name" in str(e):
                    print("Column 'confidence_value' already exists")
                else:
                    raise e

            # 4. Ensure confidentiality_score is back to VARCHAR (since it was a failed float migration)
            try:
                conn.execute(text("ALTER TABLE claims MODIFY COLUMN confidentiality_score VARCHAR(20)"))
                print("Restored 'confidentiality_score' to VARCHAR(20)")
            except Exception as e:
                print(f"Could not restore 'confidentiality_score' type: {e}")

            trans.commit()
            print("Migration completed successfully!")
        except Exception as e:
            trans.rollback()
            print(f"Migration failed: {e}")

if __name__ == "__main__":
    migrate()
